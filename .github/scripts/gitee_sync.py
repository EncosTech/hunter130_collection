#!/usr/bin/env python3
"""One-way, filtered GitHub -> Gitee mirror. Never run source project code."""

import base64
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote, urljoin

import requests


ORG_URL = re.compile(rb"(?<![A-Za-z0-9_.-])github\.com/EncosTech(?=[/?#\s\"'<>)]|$)", re.I)
MARKER = re.compile(r"\n*<!-- encostech-gitee-sync:v1 ([A-Za-z0-9+/=]+) -->\s*$")


def transform_blob(data):
    # Git's binary heuristic; never edit binary assets or LFS pointer OIDs.
    if b"\0" in data[:8192]:
        return data
    return ORG_URL.sub(b"gitee.com/EncosTech", data)


def transform_text(text):
    return transform_blob((text or "").encode()).decode()


class APIError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class API:
    def __init__(self, platform, token):
        self.platform = platform
        self.token = token
        self.base = "https://api.github.com" if platform == "github" else "https://gitee.com/api/v5"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "EncosTech-Gitee-Sync"
        if platform == "github":
            self.session.headers.update(Authorization=f"Bearer {token}", Accept="application/vnd.github+json")

    def request(self, method, path, *, params=None, data=None, files=None):
        params, data = dict(params or {}), dict(data or {})
        if self.platform == "gitee":
            if method in ("GET", "DELETE"):
                params["access_token"] = self.token
            else:
                data["access_token"] = self.token
        if files:
            return self.upload(method, path, params, data, files)
        # Retry reads only; retrying POST after a lost response could duplicate assets.
        for attempt in range(4):
            try:
                response = self.session.request(
                    method, self.base + path, params=params,
                    json=data if data and not files else None,
                    data=data if files else None, files=files, timeout=(20, 300 if files else 60),
                )
            except requests.RequestException as error:
                if method == "GET" and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                detail = str(error).replace(self.token, '***').replace(quote(self.token, safe=''), '***')
                raise RuntimeError(f"{self.platform} {method} {path}: {type(error).__name__}: {detail[:500]}") from None
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                detail = response.text[:800].replace(self.token, "***")
                raise APIError(response.status_code, f"{self.platform} {method} {path}: HTTP {response.status_code}: {detail}")
            return response.json() if response.content else None

    def upload(self, method, path, params, data, files):
        # A file-like body makes urllib3 send bounded chunks. requests' default
        # files= encoder buffers the complete attachment and sends it in one
        # SSL write, which can time out on cross-border uploads.
        boundary = 'encostech-' + uuid.uuid4().hex
        with tempfile.TemporaryFile() as body:
            for key, value in data.items():
                body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                            f'{value}\r\n').encode())
            for key, (name, stream, content_type) in files.items():
                safe_name = name.replace('\r', '%0D').replace('\n', '%0A').replace('"', '%22')
                body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{safe_name}"\r\n'
                            f'Content-Type: {content_type}\r\n\r\n').encode())
                shutil.copyfileobj(stream, body)
                body.write(b'\r\n')
            body.write(f'--{boundary}--\r\n'.encode())
            length = body.tell()
            body.seek(0)
            try:
                response = self.session.request(method, self.base + path, params=params, data=body,
                    headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'Content-Length': str(length)},
                    timeout=(120, 300))
            except requests.RequestException as error:
                detail = str(error).replace(self.token, '***').replace(quote(self.token, safe=''), '***')
                raise RuntimeError(f'{self.platform} upload {path}: {type(error).__name__}: {detail[:500]}') from None
            if not response.ok:
                detail = response.text[:800].replace(self.token, '***')
                raise APIError(response.status_code, f'{self.platform} upload {path}: HTTP {response.status_code}: {detail}')
            return response.json() if response.content else None

    def pages(self, path):
        result = []
        for page in range(1, 10001):
            batch = self.request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise RuntimeError(f"Expected paginated list: {path}")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise RuntimeError(f"Pagination limit exceeded: {path}")

    def download(self, asset, destination):
        # requests strips Authorization when GitHub redirects to its asset CDN.
        try:
            with self.session.get(asset["url"], headers={"Accept": "application/octet-stream"},
                                  stream=True, timeout=(30, 300)) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                with destination.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
        except requests.RequestException:
            raise RuntimeError(f"Failed to download GitHub asset {asset['id']}") from None
        if destination.stat().st_size != asset["size"]:
            raise RuntimeError(f"Incomplete asset download: {asset['name']}")
        expected = asset.get("digest")
        if expected and expected.startswith("sha256:") and expected != "sha256:" + digest.hexdigest():
            raise RuntimeError(f"Asset checksum mismatch: {asset['name']}")


def git(directory, *args, capture=False):
    print('git ' + ' '.join(args), flush=True)
    result = subprocess.run(["git", "-C", str(directory), '-c', 'http.lowSpeedLimit=1024',
                             '-c', 'http.lowSpeedTime=60', *args], check=True,
                            stdout=subprocess.PIPE if capture else None, text=True, timeout=1200)
    return result.stdout.strip() if capture else None


def filter_repository(directory, submodule_maps=None):
    # Full-history transformation is deterministic. Preserve commit topology and
    # messages; filtering necessarily removes cryptographic commit/tag signatures.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    mapping_file = Path(directory).parent / (Path(directory).name + '-submodules.json')
    mapping_file.write_text(json.dumps(submodule_maps or {}))
    env['GITEE_SUBMODULE_MAP'] = str(mapping_file)
    refs = git(directory, 'for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/tags', capture=True).splitlines()
    subprocess.run([
        "git", "-C", str(directory), "filter-repo", "--force", '--partial',
        "--path-regex", r"(^|/)\.github(/|$)", "--invert-paths",
        "--prune-empty", "never", "--prune-degenerate", "never",
        "--preserve-commit-hashes", "--replace-refs", "delete-no-add",
        "--blob-callback", "from gitee_sync import transform_blob\nblob.data = transform_blob(blob.data)",
        "--commit-callback", """import json, os
mapping = json.load(open(os.environ['GITEE_SUBMODULE_MAP']))
for change in commit.file_changes:
    path = change.filename.decode('utf-8', errors='surrogateescape')
    if change.mode == b'160000' and path in mapping:
        old = change.blob_id.decode()
        if old not in mapping[path]:
            raise RuntimeError('Unmapped EncosTech submodule commit: ' + path + '@' + old)
        change.blob_id = mapping[path][old].encode()
""",
        '--refs', *refs,
    ], check=True, env=env)


def submodule_mappings(mirror, scratch, cache=None, stack=()):
    """Reproduce child mirrors' deterministic commit IDs for rewritten gitlinks."""
    cache = {} if cache is None else cache
    modules = {}
    revisions = git(mirror, 'log', '--all', '--format=%H', '--', '.gitmodules', capture=True).splitlines()
    for revision in revisions:
        result = subprocess.run(['git', '-C', str(mirror), 'show', revision + ':.gitmodules'],
                                capture_output=True, text=True)
        if result.returncode:
            continue  # A commit removing .gitmodules.
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(result.stdout)
        for section in config.sections():
            url = config.get(section, 'url', fallback='').strip('"')
            path = config.get(section, 'path', fallback='').strip('"')
            if url.startswith(('../', './')):
                source_url = git(mirror, 'config', '--get', 'remote.origin.url', capture=True)
                url = urljoin(source_url.rstrip('/') + '/', url)
            match = re.fullmatch(r'(?:https?|ssh|git)://(?:git@)?github\.com/EncosTech/([A-Za-z0-9_.-]+?)(?:\.git)?/?', url, re.I)
            if match and path:
                modules.setdefault(path, set()).add(match[1])
    mapping = {}
    for path, repositories in modules.items():
        mapping[path] = {}
        for name in sorted(repositories):
            key = name.lower()
            if key in stack:
                raise RuntimeError('Cyclic EncosTech submodule dependency: ' + name)
            if key not in cache:
                child = scratch / ('submodule-' + key + '.git')
                print(f'Computing mirrored submodule commit IDs: EncosTech/{name}', flush=True)
                subprocess.run(['git', 'clone', '--bare', f'https://github.com/EncosTech/{name}.git', str(child)], check=True)
                child_maps = submodule_mappings(child, scratch, cache, (*stack, key))
                filter_repository(child, child_maps)
                commit_map = child / 'filter-repo/commit-map'
                cache[key] = dict(line.split() for line in commit_map.read_text().splitlines()[1:])
            mapping[path].update(cache[key])
    return mapping


def prepare_credentials(directory, gitee_login):
    helper = directory / "askpass.py"
    helper.write_text('''#!/usr/bin/env python3
import os, sys, re
from urllib.parse import urlsplit
prompt = sys.argv[1].lower()
match = re.search(r"https://[^'\\s]+", prompt)
host = urlsplit(match[0]).hostname if match else None
if host == "gitee.com":
    print(os.environ["GITEE_LOGIN"] if "username" in prompt else os.environ["GITEE_TOKEN"])
elif host == "github.com":
    print("x-access-token" if "username" in prompt else os.environ["GITHUB_TOKEN"])
else:
    sys.exit(1)
''')
    helper.chmod(0o700)
    os.environ.update(GIT_ASKPASS=str(helper), GIT_TERMINAL_PROMPT="0", GITEE_LOGIN=gitee_login,
                      GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="",
                      GIT_LFS_SKIP_SMUDGE="1")


def ensure_repository(gitee, source, owner):
    path = f"/repos/{quote(owner, safe='')}/{quote(source['name'], safe='')}"
    try:
        target = gitee.request("GET", path)
    except APIError as error:
        if error.status != 404:
            raise
        target = gitee.request("POST", f"/orgs/{quote(owner, safe='')}/repos", data={
            "name": source["name"], "path": source["name"], "private": source["private"],
            "description": transform_text(source.get("description")), "auto_init": False,
        })
        print(f"Created Gitee repository: {owner}/{source['name']}", flush=True)
    if source["private"] and not target.get("private", False):
        raise RuntimeError("Refusing to mirror a private GitHub repository into a public Gitee repository")
    return path, target


def check_push_hooks(gitee, target_path):
    try:
        hooks = gitee.pages(target_path + '/hooks')
    except APIError as error:
        if error.status not in (401, 403):
            raise
        print('::warning::Cannot inspect Gitee webhooks with this token; review Gitee pipeline triggers manually')
        return
    active = [str(h['id']) for h in hooks if h.get('push_events') or h.get('tag_push_events')]
    if active:
        print('::warning::Gitee push/tag webhooks exist (IDs: ' + ', '.join(active) +
              '). Removing .github does not disable externally configured pipelines; review these hooks.')
    else:
        print('Verified: no Gitee push/tag webhooks configured', flush=True)


def sync_git(gitee, source, target_path, target, directory):
    mirror = directory / "mirror.git"
    mirror.mkdir()
    git(mirror, "init", "--bare", "--initial-branch=" + source["default_branch"])
    git(mirror, "remote", "add", "origin", source["clone_url"])
    git(mirror, "fetch", "--force", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    if not git(mirror, "for-each-ref", "--format=%(refname)", "refs/heads", capture=True):
        raise RuntimeError("Source has no branches; refusing to prune the destination")
    # Keep original commits reachable in LOCAL-ONLY refs. After filtering, a
    # bitmap spanning both histories lets Git omit blobs the destination owns.
    originals = git(mirror, 'for-each-ref', '--format=%(objectname) %(refname)',
                    'refs/heads', 'refs/tags', capture=True).splitlines()
    for line in originals:
        oid, ref = line.split(' ', 1)
        git(mirror, 'update-ref', 'refs/sync-source/' + ref.removeprefix('refs/'), oid)
    maps = submodule_mappings(mirror, directory, stack=(source['name'].lower(),))
    filter_repository(mirror, maps)
    git(mirror, 'repack', '-adb')
    git(mirror, "remote", "add", "gitee", f"https://gitee.com/{target['full_name']}.git")
    # Submodule gitlinks are preserved by git-filter-repo. Never initialize or
    # execute submodules as part of mirroring the parent repository.
    lfs = git(mirror, "lfs", "ls-files", "--all", capture=True)
    if lfs:
        print("Synchronizing LFS objects", flush=True)
        # Ignore repository-supplied .lfsconfig endpoints, both for correctness
        # after URL rewriting and to restrict credentials to trusted hosts.
        source_lfs = source['clone_url'].rstrip('/') + '/info/lfs'
        target_lfs = f"https://gitee.com/{target['full_name']}.git/info/lfs"
        git(mirror, '-c', 'lfs.url=' + source_lfs, "lfs", "fetch", "--all", "origin")
        git(mirror, '-c', 'lfs.url=' + target_lfs, '-c', 'lfs.pushurl=' + target_lfs,
            "lfs", "push", "--all", "gitee")
    refspecs = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    # --partial retains original objects locally, allowing thin packs against
    # an imported destination. Only the filtered heads/tags are ever pushed.
    # Avoid chunked HTTP uploads: some Gitee/proxy paths stall on streamed packs.
    git(mirror, '-c', 'http.version=HTTP/1.1', '-c', 'http.postBuffer=268435456',
        "push", "--progress", "gitee", *refspecs)
    if target.get("default_branch") != source["default_branch"]:
        gitee.request("PATCH", target_path, data={"default_branch": source["default_branch"]})
    git(mirror, "push", "--prune", "gitee", *refspecs)
    # Verify only public Git refs. Internal GitHub PR refs are never mirrored.
    local = dict(line.split(" ", 1)[::-1] for line in git(
        mirror, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags", capture=True).splitlines())
    remote = dict(line.split("\t", 1)[::-1] for line in git(
        mirror, "ls-remote", "--refs", "gitee", "refs/heads/*", "refs/tags/*", capture=True).splitlines())
    if local != remote:
        raise RuntimeError("Destination branch/tag verification failed")
    print(f"Verified {len(local)} branch/tag refs", flush=True)


def read_manifest(body):
    match = MARKER.search(body or "")
    if not match:
        return {}
    try:
        value = json.loads(base64.b64decode(match[1]))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def fingerprint(asset):
    return {key: asset.get(key) for key in ("id", "name", "size", "updated_at", "digest")}


def sync_releases(github, gitee, source_path, target_path, directory):
    # Finish listing BOTH sides before any deletions. Do not publish GitHub drafts.
    source = [r for r in github.pages(source_path + "/releases") if not r.get("draft")]
    targets = {r["tag_name"]: r for r in gitee.pages(target_path + "/releases")}
    for release in reversed(source):
        tag = release["tag_name"]
        payload = {"tag_name": tag, "name": release.get("name") or tag,
                   "body": transform_text(release.get("body")).strip() or f"Release {tag}",
                   "prerelease": release.get("prerelease", False)}
        target = targets.get(tag)
        if target is None:
            target = gitee.request("POST", target_path + "/releases", data={
                **payload, "target_commitish": tag,
            })
        release_path = f"{target_path}/releases/{target['id']}"
        assets = github.pages(f"{source_path}/releases/{release['id']}/assets")
        existing = gitee.pages(release_path + "/attach_files")
        previous = read_manifest(target.get("body"))
        manifest = {}
        for asset in assets:
            if asset.get("state", "uploaded") != "uploaded":
                raise RuntimeError(f"GitHub asset still uploading: {asset['name']}; rerun manually")
            name = asset["name"]
            matches = [a for a in existing if a["name"] == name]
            signature = fingerprint(asset)
            if (len(matches) == 1 and previous.get(name) == {**signature, 'destination_id': matches[0]['id']}
                    and matches[0].get("size") == asset["size"]):
                manifest[name] = previous[name]
                continue
            file = directory / f"asset-{asset['id']}"
            github.download(asset, file)
            for old in matches:
                gitee.request("DELETE", f"{release_path}/attach_files/{old['id']}")
            with file.open("rb") as stream:
                print(f'Uploading {tag}/{name} ({asset["size"]} bytes)', flush=True)
                uploaded = gitee.request("POST", release_path + "/attach_files", files={
                    "file": (name, stream, "application/octet-stream"),
                })
            file.unlink()
            manifest[name] = {**signature, 'destination_id': uploaded['id']}
        for old in existing:
            if old["name"] not in manifest:
                gitee.request("DELETE", f"{release_path}/attach_files/{old['id']}")
        # Commit the asset manifest only after every transfer succeeds. A partial
        # run is retried rather than incorrectly recorded as complete.
        encoded = base64.b64encode(json.dumps(manifest, sort_keys=True).encode()).decode()
        payload["body"] += f"\n\n<!-- encostech-gitee-sync:v1 {encoded} -->"
        if any(target.get(k) != v for k, v in payload.items()):
            gitee.request("PATCH", release_path, data=payload)
        print(f"Release {tag}: {len(assets)} assets synchronized", flush=True)
    tags = {r["tag_name"] for r in source}
    for tag, release in targets.items():
        if tag not in tags:
            gitee.request("DELETE", f"{target_path}/releases/{release['id']}")
            print(f"Deleted destination-only release: {tag}", flush=True)


def main():
    for key in ("GITHUB_TOKEN", "GITEE_TOKEN", "GITHUB_REPOSITORY"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing {key}; check Actions secret repository access")
    github = API("github", os.environ["GITHUB_TOKEN"])
    gitee = API("gitee", os.environ["GITEE_TOKEN"])
    source_path = "/repos/" + os.environ["GITHUB_REPOSITORY"]
    print('Reading GitHub repository metadata', flush=True)
    source = github.request("GET", source_path)
    owner = os.environ.get("GITEE_OWNER", "EncosTech")
    print('Checking Gitee token identity and repository access', flush=True)
    identity = gitee.request("GET", "/user")
    with tempfile.TemporaryDirectory(prefix="gitee-sync-") as temp:
        directory = Path(temp)
        prepare_credentials(directory, identity["login"])
        path, target = ensure_repository(gitee, source, owner)
        check_push_hooks(gitee, path)
        sync_git(gitee, source, path, target, directory)
        sync_releases(github, gitee, source_path, path, directory)
    print(f"Synchronization complete: {source['full_name']} -> {owner}/{source['name']}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error)
        for key in ("GITHUB_TOKEN", "GITEE_TOKEN"):
            if os.environ.get(key):
                message = message.replace(os.environ[key], "***")
        print(f"::error::{message}", file=sys.stderr)
        sys.exit(1)
