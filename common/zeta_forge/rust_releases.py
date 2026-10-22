"""Read-only upstream release discovery with bounded HTTP and Git requests."""

from __future__ import annotations

import json
from http.client import HTTPException
import re
import subprocess
import tomllib
from dataclasses import dataclass
from functools import total_ordering
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .process import run_command

TIMEOUT_SECONDS = 20


@total_ordering
@dataclass(frozen=True, eq=False)
class SemVer:
    core: tuple[int, int, int]
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> SemVer:
        match = re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", text,
        )
        if match is None:
            raise RuntimeError(f"Invalid SemVer: {text!r}")
        pre = tuple(match[4].split(".")) if match[4] else ()
        if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in pre):
            raise RuntimeError(f"Invalid SemVer prerelease: {text!r}")
        return cls(tuple(int(match[i]) for i in (1, 2, 3)), pre)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SemVer) and (self.core, self.prerelease) == (other.core, other.prerelease)

    def __hash__(self) -> int:
        return hash((self.core, self.prerelease))

    def __lt__(self, other: SemVer) -> bool:
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def rust_version(text: str) -> SemVer:
    return SemVer.parse(text + ".0" if text.count(".") == 1 else text)


def index_url(name: str) -> str:
    name = name.lower()
    prefix = "1" if len(name) == 1 else ("2" if len(name) == 2 else (
        "3/" + name[0] if len(name) == 3 else name[:2] + "/" + name[2:4]
    ))
    return f"https://index.crates.io/{prefix}/{name}"


def http_text(url: str, *, missing_ok: bool = False) -> str | None:
    """urllib inherits the calling process's proxy configuration without changing it."""
    request = Request(url, headers={"User-Agent": "ZetaX-Forge-rust-updates", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"HTTP {error.code} querying {url}: {error.reason}") from error
    except (URLError, OSError, UnicodeError, HTTPException) as error:
        raise RuntimeError(f"Unable to query {url}: {error}") from error


def json_response(url: str, *, missing_ok: bool = False) -> object:
    text = http_text(url, missing_ok=missing_ok)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON from {url}: {error}") from error


def registry_releases(name: str) -> dict[str, object]:
    url = index_url(name)
    text = http_text(url)
    releases = []
    try:
        for line in text.splitlines():
            row = json.loads(line)
            if row.get("name", "").lower() != name.lower() or type(row.get("yanked")) is not bool:
                raise RuntimeError(f"Invalid crates.io index record for {name}")
            version = SemVer.parse(row["vers"])
            msrv = row.get("rust_version")
            if msrv is not None:
                rust_version(msrv)
            if not row["yanked"] and not version.prerelease:
                releases.append({"version": row["vers"], "rust_version": msrv,
                                 "url": f"https://crates.io/crates/{name}/{row['vers']}"})
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeError(f"Invalid crates.io index response for {name}: {error}") from error
    if not releases:
        raise RuntimeError(f"No unyanked stable release for {name} at {url}")
    return {"releases": releases, "sources": [url], "prereleases": [], "unverified_tags": [], "errors": []}


def remote_tags(url: str, environment: Mapping[str, str]) -> dict[str, str]:
    try:
        result = run_command(["git", "ls-remote", "--tags", url],
                             env=dict(environment, GIT_TERMINAL_PROMPT="0"),
                             capture_output=True, check=False, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Git tag query failed for {url}: {error}") from error
    if result.returncode:
        raise RuntimeError(f"Git tag query failed for {url}: {result.stderr.strip()}")
    tags, peeled = {}, {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch("[0-9a-f]{40}", fields[0]) or not fields[1].startswith("refs/tags/"):
            raise RuntimeError(f"Invalid Git tag response for {url}: {line!r}")
        commit, name = fields[0], fields[1].removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name[:-3]] = commit
        else:
            tags[name] = commit
    tags.update(peeled)
    return tags


def _release_manifest(repository: str, commit: str, path: str) -> dict[str, object]:
    url = f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path, safe='/')}"
    text = http_text(url)
    try:
        package = tomllib.loads(text)["package"]
        if not isinstance(package, dict):
            raise RuntimeError(f"Expected package table in {url}")
        inherited = [key for key in ("version", "rust-version") if isinstance(package.get(key), dict)]
        if inherited:
            workspace_text = http_text(f"https://raw.githubusercontent.com/{repository}/{commit}/Cargo.toml")
            workspace = tomllib.loads(workspace_text)["workspace"]["package"]
            for key in inherited:
                if package[key] != {"workspace": True}:
                    raise RuntimeError(f"Unsupported package inheritance in {url}: {key}")
                package[key] = workspace.get(key)
        if not isinstance(package.get("name"), str) or not isinstance(package.get("version"), str):
            raise RuntimeError(f"Missing package name/version in {url}")
        if package.get("rust-version") is not None and not isinstance(package["rust-version"], str):
            raise RuntimeError(f"Invalid package rust-version in {url}")
        return {**package, "manifest_url": url}
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(f"Invalid release manifest {url}: {error}") from error


def git_releases(name: str, entry: Mapping[str, object], environment: Mapping[str, str]) -> dict[str, object]:
    parsed = urlsplit(entry["git"])
    if parsed.netloc != "github.com" or len(parsed.path.strip("/").split("/")) != 2:
        raise RuntimeError(f"Git release discovery requires a GitHub repository for {name}")
    path, prefix = entry.get("release-manifest"), entry.get("release-tag-prefix")
    if not isinstance(path, str) or not isinstance(prefix, str):
        raise RuntimeError(f"Git release metadata missing for {name}: release-manifest/release-tag-prefix")
    repository = parsed.path.strip("/").removesuffix(".git")
    api = f"https://api.github.com/repos/{repository}/releases"
    # Enumerate all tags through Git; look up release flags per matching stable
    # tag so pagination cannot hide a draft/prerelease or a tag-only release.
    tags = remote_tags(entry["git"], environment)
    releases, prereleases, unverified, errors = [], [], [], []
    for tag, commit in sorted(tags.items()):
        if not tag.startswith(prefix):
            continue
        try:
            version = tag.removeprefix(prefix)
            parsed_version = SemVer.parse(version)
        except RuntimeError:
            unverified.append({"tag": tag, "reason": "tag does not encode a stable SemVer"})
            continue
        url = f"https://github.com/{repository}/tree/{quote(tag, safe='')}"
        if parsed_version.prerelease:
            prereleases.append({"tag": tag, "version": version, "commit": commit, "url": url})
            continue
        try:
            release = json_response(api + "/tags/" + quote(tag, safe=""), missing_ok=True)
            if release is not None:
                if (not isinstance(release, dict) or type(release.get("draft")) is not bool
                    or type(release.get("prerelease")) is not bool):
                    raise RuntimeError(f"Invalid GitHub release response for {tag}")
                if release["draft"] or release["prerelease"]:
                    continue
            manifest = _release_manifest(repository, commit, path)
            if manifest.get("name") != name or manifest.get("version") != version:
                unverified.append({"tag": tag, "commit": commit, "reason": "package name/version mismatch",
                                   "manifest": manifest["manifest_url"]})
                continue
            msrv = manifest.get("rust-version")
            if msrv is not None:
                rust_version(msrv)
            releases.append({"version": version, "commit": commit, "tag": tag, "rust_version": msrv,
                             "url": url, "manifest": manifest["manifest_url"],
                             "release": release.get("html_url") if release else None})
        except RuntimeError as error:
            errors.append(str(error))
            unverified.append({"tag": tag, "commit": commit, "reason": str(error)})
    current = _release_manifest(repository, entry["rev"], path)
    if current.get("name") != name:
        raise RuntimeError(f"Current Git manifest package does not match {name}: {current}")
    SemVer.parse(current["version"])
    return {"current_manifest": current, "releases": releases, "prereleases": prereleases,
            "unverified_tags": unverified, "errors": errors, "sources": [api, entry["git"]]}
