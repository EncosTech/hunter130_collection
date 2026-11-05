# GitCode synchronization

GitHub is authoritative. `Sync to GitCode` runs on pushes and branch/tag deletions.
Push runs wait 60 seconds for the external GitHub release publishing script.
To sync a manually edited release, open **Actions → Sync to GitCode → Run workflow**.
Manual runs do not wait. Each run reconciles all branches, tags and published
releases, including historical release attachments; GitHub drafts are not published.

The organization Actions secret `GITCODE_TOKEN` must be available to this repository.
Its GitCode account must be the personal user `EncosTech`, with repository creation, Git push, and release/
attachment API access. GitCode branch protection must permit force updates and deletion.
New repositories use the source visibility; private sources never sync to public targets.

The GitCode copy excludes all `.github/`, `.gitcode/`, and `.atomgit/` directories throughout history and replaces
`github.com/EncosTech` with `gitcode.com/EncosTech` in non-binary Git blobs and release
descriptions. Third-party links, binary files, LFS object contents, and release asset
contents stay intact. Commit IDs change and original signatures cannot be preserved.
Branch and tag names, commit authors, timestamps and topology are retained. GitCode
is a generated copy: do not develop directly there. Destination-only branches,
tags and release attachments are deleted during reconciliation. GitCode OpenAPI
does not expose whole-release deletion. If destination-only releases remain, the
action fails with their tag names and requires manual deletion on GitCode.

Git LFS objects are transferred separately before updating Git refs, using explicit
source/destination endpoints. Submodules are never checked out or executed.
EncosTech submodules with rewritten URLs or relative sibling URLs have their gitlink
commit IDs mapped using the same deterministic filter. Their GitHub repositories
must be readable by the workflow and already mirrored to GitCode. Missing commits,
cycles, or ambiguous historical module changes fail rather than publish broken refs.
Third-party submodule URLs and commit IDs stay intact. Existing externally configured
GitCode webhooks/pipelines are independent of `.github/`; the script checks push/tag
hooks and emits a warning when they exist or the token cannot inspect them.

Runs are serialized per repository; new runs never interrupt active transfers.
An HTML comment in each GitCode release stores source asset fingerprints for repeatable
incremental uploads. Failed or incomplete uploads fail the action and can be retried.
If the publisher takes more than 60 seconds, run the action manually after it finishes.

The workflow must exist on every branch that should trigger it, and on the default
branch for the manual UI. New branches created from a configured branch inherit it;
branches created from old history and newly created GitHub repositories need setup.
GitHub pushes using `GITHUB_TOKEN` and commits with CI skip annotations may not trigger
Actions; use manual dispatch for reconciliation in those cases.
