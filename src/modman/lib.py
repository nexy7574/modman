import hashlib
import importlib.metadata
import json
import logging
import math
import os
import pathlib
import shutil
import textwrap
import time
import typing

import appdirs
import httpx
import rich
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn, TaskID
from rich.progress import open as rich_open
from rich.prompt import Prompt

try:
    import h2

    HTTP2 = True
except ImportError:
    HTTP2 = False


class ModrinthAPI:
    def __init__(self):
        self.log = logging.getLogger("modman.api.modrinth")
        # use_staging = os.getenv("MODMAN_USE_STAGING_API", "0") == "1"
        try:
            _version = importlib.metadata.version("modman")
        except importlib.metadata.PackageNotFoundError:
            _version = "0.1.dev1"
        url = "https://api.modrinth.com/v2"
        # for whatever reason, the staging API doesn't work.
        self.http = httpx.Client(
            base_url=url,
            headers={
                "User-Agent": f"nexy7574/modman/{_version} (https://github.com/nexy7574/modman)",
            },
            http2=HTTP2,
            follow_redirects=True,
        )

        self.ratelimit_reset = 0
        self.ratelimit_remaining = 500
        self.project_cache = {}

    def get(self, url: str, params: dict[str, typing.Any] = None) -> dict | list:
        if self.ratelimit_remaining == 0:
            self.log.warning("Ratelimit reached, waiting %s seconds", self.ratelimit_reset)
            with Progress() as progress:
                now = time.time()
                wait_seconds = math.ceil(self.ratelimit_reset - now)
                task = progress.add_task("Waiting for rate-limit.", total=wait_seconds)
                for i in range(wait_seconds):
                    time.sleep(1)
                    progress.update(task, advance=1)
        self.log.debug(
            "Ratelimit has %d hits left, resets in %d seconds", self.ratelimit_remaining, self.ratelimit_reset
        )
        with rich.get_console().status("[cyan dim]GET " + url):
            for i in range(5):
                try:
                    response = self.http.get(url, params=params)
                except httpx.ConnectError:
                    self.log.warning("Connection error, retrying...")
                    continue
                break
            else:
                raise RuntimeError("Failed to connect to Modrinth API")
        self.ratelimit_reset = int(response.headers.get("x-ratelimit-reset", 0))
        self.ratelimit_remaining = int(response.headers.get("x-ratelimit-remaining", 100))
        if response.status_code == 429:
            self.log.warning("Request was rate-limited, re-calling.")
            return self.get(url, params)
        self.log.debug(textwrap.shorten(response.text, 10240))
        if response.status_code not in range(200, 300):
            response.raise_for_status()
        return response.json()

    def get_projects_bulk(self, project_ids: list[str]) -> list[dict[str, typing.Any]]:
        """Fetches multiple projects at once."""
        return self.get("/projects", params={"ids": json.dumps(list(project_ids))})

    def get_project(self, project_id: str) -> dict[str, typing.Any]:
        """
        Gets a project from Modrinth.

        Project ID can be the slug or the ID.
        """
        v = self.get(f"/project/{project_id}")
        self.project_cache[project_id] = v
        return v

    def get_versions(self, project_id: str, loader: str = None, game_version: str = None, release_only: bool = True):
        params = {}
        if project_id in self.project_cache:
            name = repr(self.project_cache[project_id]["title"])
        else:
            name = project_id

        if loader is not None:
            params["loaders"] = [loader]
        if game_version is not None:
            params["game_versions"] = [game_version]

        result = self.get(f"/project/{project_id}/version", params=params)
        result.sort(key=lambda v: v["date_published"], reverse=True)

        if game_version:
            for version in result.copy():
                if game_version not in version["game_versions"]:
                    result.remove(version)
                    self.log.debug(
                        "Removed %s from %s - invalid game versions (%s)",
                        version["version_number"],
                        name,
                        ", ".join(version["game_versions"]),
                    )
        else:
            self.log.debug("No game version was specified in get_versions, not filtering for game versions.")
        if loader:
            for version in result.copy():
                if loader not in version["loaders"]:
                    result.remove(version)
                    self.log.debug(
                        "Removed %s from %s - invalid loaders (%s)",
                        version["version_number"],
                        name,
                        ", ".join(version["loaders"]),
                    )
        else:
            self.log.debug("No loader was specified in get_versions, not filtering for loaders.")

        if release_only:
            real_copy = result.copy()
            for version in result.copy():
                if version["version_type"] != "release":
                    real_copy.remove(version)
                    self.log.debug("Removed %s from %s - pre-release", version["version_number"], name)
            if real_copy:
                result = real_copy
            else:
                self.log.debug("No release versions found for %s - permitting pre-release versions", name)

        self.log.debug("Got the following versions after filtering for %s: %s", name, result)
        # Finally, return the results sorted, latest to oldest
        return list(sorted(result, key=lambda v: v["date_published"], reverse=True))

    def get_versions_bulk(self, ids: typing.Iterable[str]) -> list[dict[str, typing.Any]]:
        ids_safe = json.dumps(list(ids))
        return self.get("/versions", params={"ids": ids_safe})

    def get_version(self, project_id: str, version_id: str | None):
        if version_id is None:
            self.log.info("No version specified for %s==%s, using latest.", project_id, version_id)
            versions = self.get_versions(project_id)
            return versions[0]
        return self.get(f"/project/{project_id}/version/{version_id}")

    def check_slug(self, slug_or_id: str) -> str:
        """Checks if a slug is valid, and returns the project ID."""
        return self.get(f"/project/{slug_or_id}/check")["id"]

    def get_version_from_hash(
        self, file_hash: pathlib.Path | str | None = None, algorithm: typing.Literal["sha1", "sha512"] = "sha512"
    ):
        if isinstance(file_hash, pathlib.Path):
            file = file_hash
            file_hash = hashlib.new(algorithm)
            with file.open("rb") as fd:
                while chunk := fd.read(8192):
                    file_hash.update(chunk)
            file_hash = file_hash.hexdigest()
            self.log.debug("Generated %s hash for %s: %s", algorithm, file, file_hash)

        if not isinstance(file_hash, str):
            raise TypeError("file_hash must be a string or pathlib.Path")

        return self.get(f"/version_file/{file_hash}", params={"algorithm": algorithm})

    @staticmethod
    def pick_primary_file(files: list[dict]) -> dict:
        for file in files:
            if file.get("primary", False):
                return file
        return files[0]

    def download_mod(
            self,
            version: dict,
            directory: pathlib.Path,
            *,
            progress: Progress = None,
    ):
        file = self.pick_primary_file(version["files"])
        self.log.info("Downloading %s to %s for version %r", file["filename"], directory, version["version_number"])
        if not directory.exists():
            directory.mkdir(parents=True)

        # First, check the cache directory
        cache_dir = pathlib.Path(appdirs.user_cache_dir("modman"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = fs_file = cache_dir / file["filename"]
        if cache_file.exists() is False or (cache_file.stat().st_ctime + 1209600) < time.time():
            # If it doesn't exist, download it
            self.log.info("Downloading %s - Does not exist in cache, or is stale.", file["filename"])
            with self.http.stream("GET", file["url"]) as response:
                with fs_file.open("wb") as fd:
                    if progress is None:
                        progress = Progress(
                            *Progress.get_default_columns(), DownloadColumn(os.name != "nt"), TransferSpeedColumn()
                        )
                    with progress:
                        task = progress.add_task(
                            "Downloading " + file["filename"], total=int(response.headers.get("content-length", 0))
                        )
                        for chunk in response.iter_bytes():
                            self.log.debug("Read %d bytes of %s", len(chunk), file["filename"])
                            fd.write(chunk)
                            progress.update(task, advance=len(chunk))
        else:
            self.log.info("Using cached file: %s", fs_file.resolve())

        self.log.info("Checking file hash for %s", file["filename"])
        fs_hash = hashlib.new("sha512")
        with open(fs_file, "rb") as fd:
            while chunk := fd.read(8192):
                fs_hash.update(chunk)
        if fs_hash.hexdigest() != file["hashes"]["sha512"]:
            self.log.critical("File hash mismatch.")
            self.log.debug("Hash conflict: %r on disk, %r expected", fs_hash.hexdigest(), file["hashes"]["sha512"])
            self.log.debug("Unlinking file: %s", fs_file.resolve())
            fs_file.unlink(True)
            raise RuntimeError("File hash does not match")

        # Move to the mod directory
        fs_mod = directory / file["filename"]
        self.log.debug("Copying %s -> %s", fs_file, fs_mod)
        shutil.copy(fs_file, fs_mod)
        self.log.info("Downloaded %s", file["filename"])

    def find_dependency_version_conflicts(self, project_id: str, version_id: str, config: dict) -> list[dict[str, str]]:
        conflicts = []
        for mod in config["mods"].values():
            version_info = mod["version"]
            if version_info["project_id"] != project_id:
                continue
            mod_dependencies = version_info["dependencies"]
            for dependency in mod_dependencies:
                if dependency["project_id"] == project_id:
                    if dependency["version_id"] is None:
                        self.log.info("Project %s has no version specification, assuming it supports all.", project_id)
                        continue
                    if dependency["version_id"] != version_id:
                        self.log.debug(
                            "Project %s has a conflict with %s, %s != %s",
                            project_id,
                            mod["project"]["id"],
                            version_id,
                            dependency["version_id"],
                        )
                        conflicts.append(
                            {
                                "project_id": project_id,
                                "conflict_project_id": mod["project"]["id"],
                                "version_id": version_id,
                                "conflict_version_id": dependency["version_id"],
                            }
                        )
        return conflicts

    def search(
        self,
        query: str,
        limit: int = 100,
        offset: int = 0,
        index: str = "relevance",
        *,
        versions: list[str] = None,
        project_type: list[str] = None,
        categories: list[str] = None,
        loaders: list[str] = None,
        client_side: list[str] = None,
        server_side: list[str] = None,
        open_source: bool = None,
    ) -> list[dict[str, typing.Any]]:
        """
        Searches Modrinth, returning the results.

        :param query: The actual query.
        :param limit: The maximum number of results to return, between 0 and 100. Defaults to 100.
        :param offset: The number of previous results. Defaults to 0.
        :param index: The index to search on. Defaults to "relevance". Can be "downloads", "follows", "newest",
        "updated", "relevance".
        :param versions: The game versions to search for.
        :param project_type: The project type. Only `mod` is supported at the moment.
        :param categories: The categories to search in. Can also include loaders.
        :param loaders: The loaders to look for. Gets merged with `categories`.
        :param client_side: Whether to include or exclude client-side mods.
        :param server_side: Whether to include or exclude server-side mods.
        :param open_source: Whether to include or exclude open-source mods.
        :return: A page of projects.
        """
        if project_type is None:
            project_type = ["mod"]
        else:
            if project_type != ["mod"]:
                raise ValueError("Only project_type 'mod' is supported at the moment.")
        params = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "index": index,
            "facets": [["server_side!=unsupported"]],
        }

        if versions:
            for version in versions:
                params["facets"].append([f"game_versions:{version}"])

        if project_type:
            for ptype in project_type:
                params["facets"].append([f"project_type:{ptype}"])

        if categories:
            for category in categories:
                params["facets"].append([f"categories:{category}"])

        if loaders:
            for loader in loaders:
                params["facets"].append([f"categories:{loader}"])

        if open_source is not None:
            params["facets"].append([f"open_source:{open_source}"])

        facets = json.dumps(params["facets"])
        params["facets"] = facets
        return self.get("/search", params=params)["hits"]

    def cache_get_project(self, config: dict, project_id: str) -> dict[str, typing.Any]:
        if project_id in config["mods"]:
            return config["mods"][project_id]["project"]
        self.log.debug("Project %s not found in cache, fetching from Modrinth.", project_id)
        return self.get_project(project_id)

    def cache_get_version(self, config: dict, project_id: str, version_id: str = None):
        if project_id in config["mods"]:
            return config["mods"][project_id]["version"]
        if version_id:
            self.log.debug("Version %s not found in cache, fetching from Modrinth.", version_id)
            return self.get_version(project_id, version_id)
        raise ValueError("No version specified and no cached version found.")

    def interactive_search(self, query: str, config: dict) -> dict | None:
        """
        Interactively searches Modrinth for a project.
        """
        mod_info = None
        results = self.search(
            query,
            versions=[config["modman"]["server"]["version"]],
            loaders=[config["modman"]["server"]["type"]],
            server_side=["required", "optional"],
            limit=20,
        )
        if not results:
            rich.print(f"[red]No mod with the slug, ID, or name {query!r} was found.")
            return
        elif len(results) == 1:
            self.log.info("Found mod %r by name (exact/sole match).", results[0]["title"])
            mod_info = results[0]
        else:
            while True:
                for i, item in enumerate(results, start=1):
                    rich.print(f"{i}. {item['title']} ({item['slug']})")
                p: str | int = Prompt.ask(
                    "Multiple mods were found with the name %r. Please select the one you want to install" % query
                )
                if p.isdigit():
                    p = int(p)
                    if p not in range(len(results)):
                        rich.print("[red]Invalid selection.")
                        rich.print()
                        continue
                    mod_info = results[p - 1]
                    break
                else:
                    for _mod in results:
                        if p.lower() in _mod["title"].lower() or p.lower() in _mod["slug"].lower():
                            mod_info = _mod
                            break
                    else:
                        rich.print("Invalid selection.")
                        rich.print()
                        continue

        return mod_info
