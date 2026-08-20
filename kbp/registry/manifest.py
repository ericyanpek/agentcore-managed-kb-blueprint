"""Release manifest construction and identifier derivation."""

import hashlib
import re

# 40 hex chars clears the API's 33-character minimum while keeping 160 bits of
# the digest, so truncation does not meaningfully raise collision risk.
CLIENT_TOKEN_LENGTH = 40

# A release id becomes a Step Functions execution name, whose Name shape caps at
# 80 characters. The timestamp and hash suffix consume 26, so the corpus id must
# leave room for them.
MAX_RELEASE_ID_LENGTH = 80
CORPUS_ID_PATTERN = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9-]*\Z")


def build_release_id(*, corpus_id: str, timestamp: str, corpus_sha256: str) -> str:
    """Build a release identifier.

    The corpus id is validated rather than sanitized. A silently rewritten id
    would make two distinct corpora share a release namespace, and the illegal
    character would otherwise surface far downstream: the release id has to
    satisfy the releaseId pattern in the published schema and double as a Step
    Functions execution name.

    The timestamp must already be compact (YYYYMMDDTHHMMSSZ); colons are
    rejected in both of those positions.
    """
    if not CORPUS_ID_PATTERN.match(corpus_id):
        raise ValueError(
            "corpus_id must contain only alphanumerics and hyphens and start "
            f"with an alphanumeric, got {corpus_id!r}"
        )

    release_id = f"{corpus_id}-{timestamp}-{corpus_sha256[:8]}"
    if len(release_id) > MAX_RELEASE_ID_LENGTH:
        raise ValueError(
            f"release id {release_id!r} is {len(release_id)} characters, over the "
            f"{MAX_RELEASE_ID_LENGTH}-character Step Functions execution name "
            f"limit; shorten corpus_id to at most "
            f"{MAX_RELEASE_ID_LENGTH - (len(release_id) - len(corpus_id))} characters"
        )
    return release_id


def build_client_token(*, release_id: str, operation: str, batch_index: int) -> str:
    """Derive an idempotency token that satisfies the API character set.

    The API requires 33-256 characters matching [a-zA-Z0-9](-*[a-zA-Z0-9]){0,256},
    so underscores, dots and slashes are rejected. Hashing yields hexadecimal
    output of constant length, and is deterministic so a state machine retry of
    the same batch reuses the same token.
    """
    digest = hashlib.sha256(
        f"{release_id}|{operation}|{batch_index}".encode()
    ).hexdigest()
    return digest[:CLIENT_TOKEN_LENGTH]


def build_release_manifest(
    *,
    release_id: str,
    parent_release_id: str | None,
    corpus_manifest: dict,
    change_counts: dict,
    source_commit: str,
) -> dict:
    """Wrap a corpus manifest into an immutable release manifest.

    s3VersionId is reserved on each document so a later rollback can restore the
    exact object version this release published.
    """
    documents = [
        {
            "documentId": item["documentId"],
            "file": item["file"],
            "contentSha256": item["contentSha256"],
            "metadataSha256": item["metadataSha256"],
            "s3VersionId": item.get("s3VersionId"),
        }
        for item in corpus_manifest["documents"]
    ]
    return {
        "releaseId": release_id,
        "parentReleaseId": parent_release_id,
        "corpusId": corpus_manifest["corpusId"],
        "corpusSha256": corpus_manifest["corpusSha256"],
        "sourceCommit": source_commit,
        # Counted from the array actually carried here. A count copied from the
        # input could disagree with it, and rollback trusts this number.
        "documentCount": len(documents),
        "changeCounts": change_counts,
        "status": "CANDIDATE",
        "documents": documents,
    }
