# Gitee synchronization

GitHub is authoritative. `Sync to Gitee` runs on pushes and branch/tag deletions.
Push runs wait 60 seconds for the external GitHub release publishing script.
To sync a manually edited release, open **Actions → Sync to Gitee → Run workflow**.
Manual runs do not wait. Each run reconciles all branches, tags and published
releases, including historical release attachments; GitHub drafts are not published.

The organization Actions secret `GITEE_TOKEN` must be available to this repository.
Its Gitee account needs organization repository creation, Git push, and release/
attachment API access. Gitee branch protection must permit force updates and deletion.
New repositories use the source visibility; private sources never sync to public targets.

The Gitee copy excludes all `.github/` directories throughout history and replaces
`github.com/EncosTech` with `gitee.com/EncosTech` in non-binary Git blobs and release
descriptions. Third-party links, binary files, LFS object contents, and release asset
contents stay intact. Commit IDs change and original signatures cannot be preserved.
Branch and tag names, commit authors, timestamps and topology are retained. Gitee
is a generated copy: do not develop directly there. Destination-only branches,
tags, releases and release attachments are deleted during reconciliation.

Git LFS objects are transferred separately before updating Git refs, using explicit
source/destination endpoints. Submodules are never checked out or executed.
EncosTech submodules with rewritten URLs or relative sibling URLs have their gitlink
commit IDs mapped using the same deterministic filter. Their GitHub repositories
must be readable by the workflow and already mirrored to Gitee. Missing commits,
cycles, or ambiguous historical module changes fail rather than publish broken refs.
Third-party submodule URLs and commit IDs stay intact. Existing externally configured
Gitee webhooks/pipelines are independent of `.github/`; the script checks push/tag
hooks and emits a warning when they exist or the token cannot inspect them.

Runs are serialized per repository; new runs never interrupt active transfers.
An HTML comment in each Gitee release stores source asset fingerprints for repeatable
incremental uploads. Failed or incomplete uploads fail the action and can be retried.
If the publisher takes more than 60 seconds, run the action manually after it finishes.

The workflow must exist on every branch that should trigger it, and on the default
branch for the manual UI. New branches created from a configured branch inherit it;
branches created from old history and newly created GitHub repositories need setup.
GitHub pushes using `GITHUB_TOKEN` and commits with CI skip annotations may not trigger
Actions; use manual dispatch for reconciliation in those cases.
