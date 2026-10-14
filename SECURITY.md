# Security and privacy

This project backs up user-selected files to a **private** GitHub repository. Its public source repository must never receive real backup data, access tokens, local configuration, recovery keys, logs, indexes, or screenshots containing personal paths.

## Design

- AES-256-GCM with a random key and per-package random nonce. Decryption does not publish plaintext until the authentication tag verifies.
- Remote package sizes and SHA-256 digests must match before the successful index advances. GCM provides authenticity; SHA-256 detects transport/storage mismatches.
- A state directory is bound to a repository name, repository ID when configured, and a key fingerprint. A process lock prevents concurrent writers. Restore receipts enforce incremental ordering.
- Repository addresses are restricted to GitHub.com. Credentials remain in GitHub CLI's credential store and are never embedded in configuration. Authenticated API calls do not follow arbitrary redirects.
- Restore paths, asset names, identifiers, chunk offsets, overlaps, complete file hashes, and symbolic links are checked before restoring data. Deletions are reported, not applied.

## Limits

This is not an independently audited cryptographic product. It cannot protect against a compromised local account, stolen recovery key, malicious source files altered during reads without detectable metadata changes, or all filesystem races. Plaintext staging exists locally while a job is pending; access restrictions are not full-disk encryption. Secure deletion is not implemented. Metadata such as encrypted asset sizes, version times, and counts is visible to GitHub and repository collaborators.

GitHub privacy is checked before uploads and finalization, but the application cannot prevent an authorized repository administrator from making a repository public later. Keep repository access restricted. Secret-file exclusion is a convenience filter, not a data-loss-prevention guarantee.

Do not attach real backups, logs, keys, configuration files, or account tokens to public issues. Report reproducible security defects with minimal synthetic examples. If a private reporting channel is available on the repository, use it for details that should not be public.
