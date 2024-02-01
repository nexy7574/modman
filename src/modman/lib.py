import os
import pathlib
import shutil
import typing
import hashlib
import rich
import appdirs
import time
import math
from rich.progress import track, open as rich_open, Progress, DownloadColumn, TransferSpeedColumn

import httpx
import logging
import importlib.metadata

try:
    import h2
    HTTP2 = True
except ImportError:
    HTTP2 = False


class ModrinthAPI:
    def __init__(self):
        self.log = logging.getLogger("modman.api.modrinth")
        try:
            _version = importlib.metadata.version("modman")
        except importlib.metadata.PackageNotFoundError:
            _version = "1.0.0.dev1"
        self.http = httpx.Client(
            base_url="https://api.modrinth.com/v2",
            headers={
                "User-Agent": f"modman/{_version} (https://github.com/nexy7574/modman)",
            },
            http2=HTTP2,
            follow_redirects=True
        )

        self.ratelimit_reset = 0
        self.ratelimit_remaining = 0

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
        with rich.get_console().status("GET " + url):
            response = self.http.get(url, params=params)
        self.ratelimit_reset = int(response.headers.get("x-ratelimit-reset", 0))
        self.ratelimit_remaining = int(response.headers.get("x-ratelimit-remaining", 0))
        logging.debug(response.text)
        if response.status_code not in range(200, 300):
            response.raise_for_status()
        return response.json()

    def get_project(self, project_id: str):
        return self.get(f"/project/{project_id}")

    def get_versions(self, project_id: str, loader: str = None, game_version: str = None):
        params = {}
        if loader is not None:
            params["loaders"] = [loader]
        if game_version is not None:
            params["game_versions"] = [game_version]
        return self.get(f"/project/{project_id}/version", params=params)

    def get_version(self, project_id: str, version_id: str | None):
        if version_id is None:
            self.log.warning("No version specified, using latest.")
            versions = self.get_versions(project_id)
            return versions[0]
        return self.get(f"/project/{project_id}/version/{version_id}")

    def get_version_from_hash(
            self,
            file_hash: pathlib.Path | str | None = None,
            algorithm: typing.Literal["sha1", "sha512"] = "sha512"
    ):
        if isinstance(file_hash, pathlib.Path):
            file = file_hash
            file_hash = hashlib.new(algorithm)
            with file.open("rb") as fd:
                while chunk := fd.read(8192):
                    file_hash.update(chunk)
            file_hash = file_hash.hexdigest()

        if not isinstance(file_hash, str):
            raise TypeError("file_hash must be a string or pathlib.Path")

        return self.get(
            f"/version_file/{file_hash}",
            params={"algorithm": algorithm}
        )

    @staticmethod
    def pick_primary_file(files: list[dict]) -> dict:
        for file in files:
            if file.get("primary", False):
                return file
        return files[0]

    def download_mod(self, version: dict, directory: pathlib.Path):
        file = self.pick_primary_file(version["files"])
        if not directory.exists():
            directory.mkdir(parents=True)

        # First, check the cache directory
        cache_dir = pathlib.Path(appdirs.user_cache_dir("modman"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not (fs_file := cache_dir / file["filename"]).exists():
            # If it doesn't exist, download it
            self.log.info("Downloading %s", file["filename"])
            with self.http.stream("GET", file["url"]) as response:
                with fs_file.open("wb") as fd:
                    with Progress(
                        *Progress.get_default_columns(),
                        DownloadColumn(os.name != "nt"),
                        TransferSpeedColumn()
                    ) as progress:
                        task = progress.add_task(
                            "Downloading " + file["filename"],
                            total=int(response.headers.get("content-length", 0))
                        )
                        for chunk in response.iter_bytes():
                            fd.write(chunk)
                            progress.update(task, advance=len(chunk))

        self.log.info("Checking file hash")
        fs_hash = hashlib.new("sha512")
        with rich_open(fs_file, "rb", description="Generating SHA512 sum", transient=True) as fd:
            while chunk := fd.read(8192):
                fs_hash.update(chunk)
        if fs_hash.hexdigest() != file["hashes"]["sha512"]:
            self.log.critical("File hash mismatch.")
            fs_file.unlink(True)
            raise RuntimeError("File hash does not match")

        # Move to the mod directory
        fs_mod = directory / file["filename"]
        self.log.debug("Moving %s -> %s", fs_file, fs_mod)
        shutil.move(fs_file, fs_mod)
        self.log.info("Downloaded %s", file["filename"])

    def find_dependency_conflicts(self, project_id: str, version_id: str, config: dict) -> list[dict[str, str]]:
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
                        conflicts.append(
                            {
                                "project_id": project_id,
                                "conflict_project_id": mod["project"]["id"],
                                "version_id": version_id,
                                "conflict_version_id": dependency["version_id"]
                            }
                        )
        return conflicts
