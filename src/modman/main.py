import datetime
import enum
import json
import logging
import os
import random
import time
import zipfile
from importlib.metadata import version as importlib_version
from pathlib import Path
from threading import Thread

import appdirs
import click
import httpx
import packaging.version
import rich
from click_aliases import ClickAliasedGroup
from packaging.version import parse as parse_version
from rich.layout import Layout
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn
from rich.table import Table
from rich.traceback import install

from .lib import ModrinthAPI


def release_is_newer(r_type: str, than: str) -> bool:
    """
    Checks that r_type is a newer or equal version to than.
    """
    mapping = {"release": 2, "beta": 1, "alpha": 0}
    return mapping[r_type] >= mapping[than]

# aikars_flags = [
#     "-XX:+UseG1GC",
#     "-XX:+ParallelRefProcEnabled",
#     "-XX:MaxGCPauseMillis=200",
#     "-XX:+UnlockExperimentalVMOptions",
#     "-XX:+DisableExplicitGC",
#     "-XX:+AlwaysPreTouch",
#     "-XX:G1NewSizePercent=30",
#     "-XX:G1MaxNewSizePercent=40",
#     "-XX:G1HeapRegionSize=8M",
#     "-XX:G1ReservePercent=20",
#     "-XX:G1HeapWastePercent=5",
#     "-XX:G1MixedGCCountTarget=4",
#     "-XX:InitiatingHeapOccupancyPercent=15",
#     "-XX:G1MixedGCLiveThresholdPercent=90",
#     "-XX:G1RSetUpdatingPauseTimePercent=5",
#     "-XX:SurvivorRatio=32",
#     "-XX:+PerfDisableSharedMem",
#     "-XX:MaxTenuringThreshold=1",
#     "-Dusing.aikars.flags=https://mcflags.emc.gs",
#     "-Daikars.new.flags=true",
# ]


logger = logging.getLogger("modman")

app_dir = Path(appdirs.user_cache_dir("modman"))
app_dir.mkdir(parents=True, exist_ok=True)
# Create the file debug stream
file_handler = logging.FileHandler(app_dir / "modman.log")
file_handler.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s"))
file_handler.setLevel(logging.DEBUG)
logger.addHandler(file_handler)


def load_config() -> tuple[dict, Path]:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        p = parent / ".modman.json"
        if not p.exists():
            continue
        else:
            break
    else:
        logger.warning(
            f"No ModMan configuration file found (or in any parent up to {cwd.root}). You may need to run `modman init`"
        )
        raise click.Abort("No modman.json found.")

    with open(p) as fd:
        data = json.load(fd)

    # Migrations
    if "root" not in data["modman"]:
        logger.warning(f"Migrating modman.json - Adding root={p.parent!s}")
        data["modman"]["root"] = str(p.parent)
        with open(p, "w") as fd:
            json.dump(data, fd, indent=4)
    if "file" not in data["modman"]["server"]:
        logger.warning("Modman meta file is out of date, attempting to migrate")
        root = Path(data["modman"]["root"])
        if not root.exists():
            logger.critical(
                "Unable to migrate - root path %r does not exist anymore. Please re-run `modman init`.",
                str(root),
            )
            raise click.Abort("Root path does not exist.")
        for file in root.glob("*.jar"):
            v = detect_server_version(file)
            if v:
                data["modman"]["server"]["type"], data["modman"]["server"]["version"] = v
                data["modman"]["server"]["file"] = str(file.resolve())
                break
        else:
            logger.critical("Unable to migrate - cannot locate server binary. Please re-run `modman init`.")
            raise click.Abort("Server binary not found.")
    return data, Path(data["modman"]["root"])


def detect_server_version(file: Path) -> tuple[str, str] | None:
    """
    Detects the type and version of server at the given destination.

    Return a tuple of (server_type, server_version), or None if it could not be detected.
    """
    server_type = server_version = None
    with zipfile.ZipFile(file) as _zip:
        if "install.properties" in _zip.namelist():
            with _zip.open("install.properties") as fd:
                install_properties = fd.read().decode("utf-8").splitlines()
                for line in install_properties:
                    if line.startswith("fabric-loader-version="):
                        server_type = "fabric"
                    elif line.startswith("game-version="):
                        server_version = line.split("=")[1]
    if server_type and server_version:
        return server_type, server_version


@click.group("modman", invoke_without_command=True, cls=ClickAliasedGroup)
@click.option(
    "--log-level",
    "-L",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default="WARNING",
    envvar="MODMAN_LOG_LEVEL",
)
@click.option(
    "--log-file",
    "-l",
    type=click.Path(),
    default=str(app_dir / "modman.log"),
    envvar="MODMAN_LOG_FILE",
)
@click.option("_version", "--version", "-V", is_flag=True, help="Prints the version of modman and checks for updates.")
@click.pass_context
def main(ctx: click.Context, log_level: str, log_file: str | None, _version: bool):
    if log_file is None:
        log_file = Path(appdirs.user_cache_dir("modman")) / "modman.log"
    if log_level.upper() == "DEBUG":
        install(show_locals=True)
    # Add a console output stream for log_level
    stream_handler = RichHandler(show_path=False)
    stream_handler.setLevel(logging.getLevelName(log_level.upper()))
    logger.addHandler(stream_handler)
    logger.setLevel(logging.DEBUG)

    logging.getLogger("hpack.hpack").setLevel("INFO")
    logging.getLogger("httpcore.http2").setLevel("INFO")
    logger.debug("Silenced hpack.hpack and httpcore.http2 logs as they've way too verbose.")

    if log_file:
        handler = logging.FileHandler(log_file)
        handler.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s"))
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)

    if (mods_dir := Path.cwd() / "mods").exists():
        logger.debug("Found mods directory at %s", mods_dir)
        logger.debug("Contents: %s", ", ".join(str(x) for x in mods_dir.iterdir()))

    last_update_file = Path(appdirs.user_cache_dir("modman")) / ".last_update_ts"
    if last_update_file.exists():
        last_update_ts = float(last_update_file.read_text() or "0.0")
    else:
        last_update_ts = 0.0
    if _version or (time.time() - last_update_ts) > 806400:
        mm_version = parse_version(importlib_version("modman"))
        logger.info("ModMan Version: %s", mm_version)
        if _version:
            rich.print("ModMan is running version %s" % mm_version)
        logger.debug("Checking for updates...")
        local_version = mm_version.local
        if not local_version.startswith("g"):
            logger.debug("Local version is not a git release. Update check is not yet implemented.")
        else:
            commit_hash = local_version[1:8]
            commits_git = httpx.get("https://api.github.com/repos/nexy7574/modman/commits")
            if commits_git.status_code != 200:
                logger.warning("Could not check for updates: %s", commits_git.text)
            else:
                last_update_file.write_text(str(time.time()))
                commits = commits_git.json()
                latest_commit = commits[0]
                if latest_commit["sha"][:7] != commit_hash:
                    n = 0
                    for commit in commits:
                        if commit["sha"][:7] == commit_hash:
                            break
                        n += 1
                    else:
                        n = -1

                    if n > -1:
                        logger.warning(
                            "You are not running the latest version of ModMan. You are on %s (%d commits behind), "
                            "the latest is %s",
                            commit_hash,
                            n,
                            latest_commit["sha"][:7],
                        )
                    else:
                        # Probably a dev version
                        logger.warning("You do not appear to be running a tracked version of modman.")
                else:
                    logger.info("You are running the latest version of ModMan.")

    if ctx.invoked_subcommand is not None:
        logger.debug(
            "Running command: %s with args %r and kwargs %r",
            ctx.invoked_subcommand,
            ctx.args,
            ctx.params,
        )


@main.command("init")
@click.option("--name", "-n", default=Path.cwd().name)
@click.option("--auto/--no-auto", "-A/-N", default=True, help="Automatically detects installed mods.")
@click.argument("server_type", type=click.Choice(["fabric", "forge", "auto"], case_sensitive=False), default="auto")
@click.argument("server_version", type=str, default="auto")
def init(name: str, auto: bool, server_type: str, server_version: str):
    """Creates a modman project in the current directory.

    SERVER_VERSION should be the minecraft version of the server, for example, 1.20.2, or 23w18a.

    In order for auto-detection to work, you must already have a ./mods/ directory."""
    if server_type == "forge":
        logger.warning("Forge is not fully supported.")
    if auto is False and (server_type == "auto" or server_version == "auto"):
        logger.error("'--no-auto' and '--server-type/-version auto' are mutually exclusive.")
        raise click.Abort()

    config_data = {
        "modman": {"name": name, "server": {"type": server_type, "version": server_version}, "root": str(Path.cwd())},
        "mods": {},
    }

    api = ModrinthAPI()

    if auto:
        jars = list(Path.cwd().glob("*.jar"))
        if len(jars) > 1:
            logger.warning("Multiple jar files found. Auto-detection may not work as expected.")
        for jar in Path.cwd().glob("*.jar"):
            logger.debug("Inspecting jar %r", jar)
            v = detect_server_version(jar)
            if v:
                config_data["modman"]["server"]["type"], config_data["modman"]["server"]["version"] = v
                config_data["modman"]["server"]["file"] = str(jar.resolve())
                break
        else:
            logger.warning("Could not detect server version. Please specify it manually.")
            raise click.Abort()
        if not config_data["modman"]["server"]["version"]:
            raise click.Abort("Could not detect server version. Please specify it manually.")

        mods_dir = Path.cwd() / "mods"
        if not mods_dir.exists():
            logger.critical("'mods' directory does not exist. Cannot auto-detect mods.")
            return

        to_get = {}
        for mod in mods_dir.iterdir():
            if mod.is_dir():
                continue

            try:
                mod_version_info = api.get_version_from_hash(mod)
            except httpx.HTTPStatusError:
                logger.info(f"File {mod} does not appear to be a mod, or it is not on modrinth.")
                continue
            else:
                to_get[mod_version_info["project_id"]] = {
                    "project": {},
                    "version": mod_version_info,
                    "file": mod
                }
        projects = api.get_projects_bulk(list(to_get.keys()))
        for mod_info in projects:
            mod_version_info = to_get[mod_info["id"]]["version"]
            mod = to_get[mod_info["id"]]["file"]
            primary_file = ModrinthAPI.pick_primary_file(mod_version_info["files"])["filename"]
            logger.info(f"Detected {mod_info['title']!r} version {mod_version_info['name']!r} from {mod}")
            if mod.name != primary_file:
                logger.warning(
                    "File %r does not match primary file name %r. Renaming it.",
                    mod,
                    primary_file,
                )
                mod.rename(mod.with_name(primary_file))
            config_data["mods"][mod_info["slug"]] = {
                "project": mod_info,
                "version": mod_version_info,
            }
    if config_data["modman"]["server"]["type"] == "auto":
        logger.error("Could not detect server type. Please specify it manually.")
        raise click.Abort()
    if config_data["modman"]["server"]["version"] == "auto":
        logger.error("Could not detect server version. Please specify it manually.")
        raise click.Abort()

    with open(".modman.json", "w+") as fd:
        json.dump(config_data, fd, indent=4)
    server_type = config_data["modman"]["server"]["type"]
    server_version = config_data["modman"]["server"]["version"]
    rich.print(f"[green]Detected server: {server_type}, {server_version}")
    table = Table("Mod name", "Installed version", "File", title="Detected Mods")
    for mod in config_data["mods"].values():
        file = Path.cwd() / "mods" / ModrinthAPI.pick_primary_file(mod["version"]["files"])["filename"]
        table.add_row(
            mod["project"]["title"],
            mod["version"]["name"],
            str(file),
        )
    rich.print(table)
    rich.print("[green]Created modman.json.")


@main.command("install", aliases=["add"])
@click.argument("mods", type=str, nargs=-1)
@click.option("--reinstall", "-R", is_flag=True, help="Whether to reinstall already installed mods.")
@click.option("--optional/--no-optional", "-O/-N", default=False, help="Whether to install optional dependencies.")
@click.option("--dry", "-D", is_flag=True, help="Whether to simulate the installation.")
def install_mod(mods: tuple[str], optional: bool, reinstall: bool, dry: bool):
    """Installs a mod."""
    config, root = load_config()
    if not mods:
        mods = config["mods"].keys()
        if not mods:
            logger.critical("No mods specified. Did you mean `modman init`?")
            return
    api = ModrinthAPI()
    collected_mods = []
    for mod in mods:
        if "==" in mod:
            mod, version = mod.split("==")
        else:
            version = "latest"
        try:
            mod_info = api.get_project(mod)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning("Mod %r is invalid - searching by name", mod)
                mod_info = None
                while mod_info is None:
                    try:
                        mod_info = api.interactive_search(mod, config)
                        mod_info = api.get_project(mod_info["slug"])
                    except KeyboardInterrupt:
                        raise click.Abort()
            else:
                raise

        if mod_info["slug"] in config["mods"]:
            logger.info("Mod %s is already installed. Did you mean `modman update`?" % mod_info["title"])
            if reinstall is False:
                continue
            version = config["mods"][mod_info["slug"]]["version"]["id"]
        if mod_info.get("server_side") == "unsupported":
            logger.warning("Mod %s is client-side only, you may not see an effect.", mod_info["title"])

        if version == "latest":
            logger.info("Version was set to 'latest'. Finding latest version.")
            versions = api.get_versions(
                mod_info["id"],
                loader=config["modman"]["server"]["type"],
                game_version=config["modman"]["server"]["version"],
            )
            if not versions:
                logger.critical(
                    "Mod %s does not support %s (no versions).",
                    mod_info["title"],
                    config["modman"]["server"]["version"],
                )
                continue
            logger.debug("Found versions: %r", versions)
            version_info = versions[0]
            logger.debug("Selected version %r as it is the first index.", version_info["name"])
        else:
            logger.debug("Specific version %r requested. Finding version.")
            version_info = api.get_version(mod_info["id"], version)
            logger.debug("Found version %r: %r", version_info["name"], version_info)
        if config["modman"]["server"]["version"] not in version_info["game_versions"]:
            logger.warning(
                "Mod %s does not support %s, only %s.",
                mod_info["title"],
                config["modman"]["server"]["version"],
                ", ".join(version_info["game_versions"]),
            )
            # continue
        if config["modman"]["server"]["type"] not in version_info["loaders"]:
            logger.warning("Mod %s does not support %s.", mod_info["title"], config["modman"]["server"]["type"])
            continue
        logger.debug("Adding %s==%s to the queue.", mod_info["title"], version_info["name"])
        collected_mods.append({"mod": mod_info, "version": version_info})

    # Resolve dependencies
    queue = [*collected_mods]
    for mod in collected_mods:
        mod_info = mod["mod"]
        version_info = mod["version"]
        logger.debug("Resolving dependencies for %s==%s", mod_info["title"], version_info["name"])
        for dependency_info in version_info["dependencies"]:
            if dependency_info["dependency_type"] == "optional" and not optional:
                logger.info(
                    "%s depends on %s==%s, but it is optional. Skipping.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"],
                )
                continue
            else:
                logger.info(
                    "%s depends on %s==%s, finding version.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"],
                )
            dependency = api.get_project(dependency_info["project_id"])
            if dependency_info["version_id"] is None:
                # Assume latest version
                versions = api.get_versions(
                    dependency_info["project_id"],
                    loader=config["modman"]["server"]["type"],
                    game_version=config["modman"]["server"]["version"],
                )
                if not versions:
                    logger.critical(
                        "Mod %s does not support %s (no versions).",
                        dependency["title"],
                        config["modman"]["server"]["version"],
                    )
                    continue
                logger.debug(
                    "Got versions [%s], picking index 0 (supposedly the latest): %s",
                    ", ".join(v["name"] for v in versions),
                    versions[0]["name"],
                )
                dependency_version = versions[0]
            else:
                dependency_version = api.get_version(dependency_info["project_id"], dependency_info["version_id"])
            logger.info(
                "%s depends on %s==%s", mod_info["title"], dependency["title"], dependency_version["version_number"]
            )
            logger.info("Checking for dependency version conflicts.")
            conflicts = api.find_dependency_version_conflicts(mod_info["id"], version_info["id"], config)
            if conflicts:
                logger.warning("Found dependency conflicts: %s", conflicts)
            logger.debug("Adding %s==%s to the queue.", dependency["title"], dependency_version["name"])
            queue.append({"mod": dependency, "version": dependency_version})

    # Resolve conflicts
    for item in queue.copy():
        mod_info = item["mod"]
        version_info = item["version"]

        for dependency in version_info["dependencies"]:
            if dependency["dependency_type"] == "incompatible":
                rich.print("[red]Mod %r is incompatible with %r." % (mod_info["title"], dependency["project_id"]))

    table = Table("Mod", "Version", title="Installing Mods")
    v = 0
    for item in queue:
        _mod_info = item["mod"]
        _version_info = item["version"]
        logger.debug("Downloading %s==%s", _mod_info["title"], _version_info["name"])
        if dry is False:
            api.download_mod(_version_info, root / "mods")
        table.add_row(_mod_info["title"], _version_info["name"])
        v += 1
        config["mods"][_mod_info["slug"]] = {"project": _mod_info, "version": _version_info}

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)

    if v:
        rich.print(table)
        rich.print("[green]Done.")
    else:
        rich.print("[yellow]:warning: No changes made. All mods are already installed, or unavailable.")


@main.command("update", aliases=["upgrade"])
@click.argument("mods", type=str, nargs=-1)
@click.option("--game-version", "--server-version", "-V", type=str, default=None, help="The game version to update to.")
@click.option(
    "--pre-releases",
    "--pre",
    "-P",
    default=False,
    is_flag=True,
    help="Whether to allow updating to alpha/beta versions. By default, will only upgrade to releases, unless"
         " a pre-release is already installed."
)
def update_mod(mods: tuple[str], game_version: str = None, pre_releases: bool = False):
    """Updates one or more mods.

    If no mods are specified, all mods will be updated.

    If a mod is specified, all of its dependencies will be updated too.

    If a mod does not have any updates available, it will be skipped.
    """

    def get_installed_project(mod_slug: str) -> dict | None:
        for _key, _data in config["mods"].items():
            if _key == mod_slug:
                return _data["project"]
            elif _data["project"]["id"] == mod_slug:
                return _data["project"]
        return

    def get_installed_version(mod_slug: str) -> dict | None:
        for _key, _data in config["mods"].items():
            if _key == mod_slug:
                return _data["version"]
            elif _data["project"]["id"] == mod_slug:
                return _data["version"]
        raise ValueError(f"Mod {mod_slug} is not installed.")

    api = ModrinthAPI()
    config, root = load_config()
    if game_version is None:
        game_version = config["modman"]["server"]["version"]
    game_loader = config["modman"]["server"]["type"]

    if not mods:
        logger.info("No mods specified. Updating all mods.")
        mods = config["mods"].keys()

    mods = list(mods)
    projects = {}
    for project in mods.copy():
        result = get_installed_project(project)
        if result:
            projects[result["slug"]] = result
            mods.remove(project)
        else:
            logger.warning("Mod %r is not installed.", project)

    if not projects:
        logger.warning("No valid mods specified.")
        return

    changes = {}

    all_projects = api.get_projects_bulk(list(projects.keys()))
    logger.debug("Got %d projects.", len(all_projects))
    _all_version_ids = []
    for project in all_projects:
        _all_version_ids.extend(project["versions"][-5:])
    all_versions = api.get_versions_bulk(_all_version_ids)

    for version in all_versions:
        project = get_installed_project(version["project_id"])
        installed_version = get_installed_version(version["project_id"])
        installed_datetime = datetime.datetime.fromisoformat(installed_version["date_published"])
        version_release_datetime = datetime.datetime.fromisoformat(version["date_published"])
        if version_release_datetime <= installed_datetime:
            logger.debug("Release %r was older than the currently installed version, ignoring.", version["name"])
            continue

        release_type = version["version_type"]
        if not release_is_newer(release_type, installed_version["version_type"]):
            logger.debug("Release %r is not newer than the installed version, ignoring.", version["name"])
            continue
        elif release_type != "release" and pre_releases is False and installed_version["version_type"] == "release":
            logger.debug("Release %r is a pre-release, but pre-releases are disabled, ignoring.", version["name"])
            continue

        if game_loader not in version["loaders"]:
            logger.debug("Release %r does not support loader %r, ignoring.", version["name"], game_loader)
            continue

        if version["game_versions"] and game_version not in version["game_versions"]:
            logger.debug("Release %r does not support game version %r, ignoring.", version["name"], game_version)
            continue

        changes[project["slug"]] = {
            "project": project,
            "installed_version": installed_version,
            "new_version": version
        }

    if not changes:
        logger.info("No updates available.")
        return

    progress = Progress(
        *Progress.get_default_columns(),
        DownloadColumn(os.name != "nt"),
        TransferSpeedColumn(),
    )
    download_tasks = []
    for slug, metadata in changes.items():
        project = metadata["project"]
        installed_version = metadata["installed_version"]
        new_version = metadata["new_version"]
        logger.info(
            "Updating %s from %s to %s",
            project["title"],
            installed_version["name"],
            new_version["name"],
        )
        t = Thread(
            target=lambda: api.download_mod(new_version, root / "mods", progress=progress),
        )
        t.start()
        download_tasks.append(
            (t, metadata)
        )
    for thread, _ in download_tasks:
        thread.join()
    progress.refresh()

    table = Table("Mod", "Old Version", "New Version", title="Updated Mods")
    for _, metadata in download_tasks:
        project = metadata["project"]
        installed_version = metadata["installed_version"]
        new_version = metadata["new_version"]
        table.add_row(project["title"], installed_version["name"], new_version["name"])
        config["mods"][project["slug"]] = {"project": project, "version": new_version}

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)
    rich.print(table)
    rich.print("[green]Done.")


@main.command("uninstall", aliases=["remove", "del", "delete"])
@click.argument("mods", type=str, nargs=-1)
@click.option("--purge", "-P", is_flag=True, help="Whether to delete dependencies too.")
def uninstall(mods: tuple[str], purge: bool):
    """Properly deletes & uninstalls a mod."""
    if not mods:
        rich.print("[red]No mods specified.")
        return
    config, root = load_config()

    mod_identifiers = {}
    for key, mod in config["mods"].items():
        mod_identifiers[key] = [
            mod["project"]["id"],
            mod["project"]["title"],
            mod["project"]["slug"],
            ModrinthAPI.pick_primary_file(mod["version"]["files"])["filename"],
            key,
        ]

    identifiers_flat = [item for value_pack in mod_identifiers.values() for item in value_pack]
    logger.debug("Mod identifiers mapping: %r", mod_identifiers)
    logger.debug("Flat mod identifiers array: %r", identifiers_flat)
    for mod in mods:
        if Path(mod).exists():
            mod = Path(mod).name
        if mod not in identifiers_flat:
            rich.print(f"[red]Mod {mod} is not installed.")
            continue
        else:
            for key, values in mod_identifiers.items():
                if mod in values:
                    mod = key
                    break
        mod_info = config["mods"][mod]
        if purge:
            for dependency_info in mod_info["version"]["dependencies"]:
                if dependency_info["dependency_type"] == "optional":
                    continue
                # Make sure nothing else depends on it first
                for other_mod in config["mods"].values():
                    for other_dependency in other_mod["version"]["dependencies"]:
                        if other_dependency["project_id"] == dependency_info["project_id"]:
                            break
                    else:
                        continue
                    break
                else:
                    dependency = config["mods"][dependency_info["project_id"]]
                    rich.print(f"Uninstalling dependency {dependency['project']['title']}")
                    primary_file = ModrinthAPI.pick_primary_file(dependency["version"]["files"])
                    fs_file = root / "mods" / primary_file["filename"]
                    try:
                        fs_file.unlink(True)
                    except OSError as e:
                        logger.warning("Could not remove dependency file %s (uninstalling): %s", fs_file.resolve(), e)
                    del config["mods"][dependency_info["project_id"]]
        rich.print(f"Uninstalling mod {mod_info['project']['title']}")
        primary_file = ModrinthAPI.pick_primary_file(mod_info["version"]["files"])
        file = root / "mods" / primary_file["filename"]
        try:
            logger.debug("Removing file %s (uninstalling)", file)
            file.unlink(True)
        except OSError as e:
            logger.warning("Could not remove file %s: %s", file, e, exc_info=True)
        del config["mods"][mod]

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)

    rich.print("[green]Done.")


@main.command("list")
def list_mods():
    """Lists all installed mods and their version."""
    config, root = load_config()
    table = Table("Mod", "Version", "File", title=f"Installed Mods (Minecraft {config['modman']['server']['version']})")
    for mod in config["mods"].values():
        file = root / "mods" / ModrinthAPI.pick_primary_file(mod["version"]["files"])["filename"]
        if not file.exists():
            logger.warning(
                "File %s does not exist. Was it deleted? Try `modman install -R %s`", file, mod["project"]["slug"]
            )
        table.add_row(
            mod["project"]["title"], mod["version"]["name"], str(file.resolve()), style="" if file.exists() else "red"
        )
    rich.print(table)


@main.command("pack")
@click.option("--server-side", "-S", is_flag=True, help="Whether to include server-side mods.")
def create_pack(server_side: bool):
    """Creates a modpack zip to send to people using the server

    This will only include client-side mods by default. You can include all mods with the -S flag."""
    config, root = load_config()
    if not (root / "mods").exists():
        logger.critical("No mods directory found. Are you in the right directory?")
        return
    output_zip = root / (config["modman"]["name"] + ".zip")

    with zipfile.ZipFile(output_zip, "w", compresslevel=9) as _zip:
        for mod in config["mods"].values():
            if server_side is False and mod["project"]["client_side"] == "unsupported":
                logger.info("Skipping server-side mod %s", mod["project"]["title"])
                continue
            primary_file = ModrinthAPI.pick_primary_file(mod["version"]["files"])
            logger.info("Appending %s", primary_file["filename"])
            _zip.write(root / "mods" / primary_file["filename"], primary_file["filename"])
    rich.print("[green]Created ZIP at %s" % output_zip)


@main.command("download-fabric")
@click.argument("game_version")
@click.argument("loader_version", required=False)
@click.argument("installer_version", required=False)
def download_fabric(game_version: str, loader_version: str | None, installer_version: str | None):
    """Downloads a Fabric server.

    GAME_VERSION should be the minecraft version of the server, for example, 1.20.2, or 23w18a.

    If LOADER_VERSION or INSTALLER_VERSION are not specified, the latest version will be used. This is recommended."""
    loader_version = loader_version or "latest"
    installer_version = installer_version or "latest"
    api = ModrinthAPI()
    if game_version == "latest":
        response = api.get("https://meta.fabricmc.net/v2/versions/game/intermediary")
        for version in response:
            if version["stable"]:
                game_version = version["version"]
                break
        else:
            logger.critical("Could not find a stable minecraft version.")
            return

    game_is_stable = game_version.count(".") == 2

    if loader_version == "latest":
        response = api.get(f"https://meta.fabricmc.net/v2/versions/loader/{game_version}")
        response = list(filter(lambda x: x["loader"]["stable"] in (game_is_stable, True), response))
        response.sort(key=lambda x: packaging.version.parse(x["loader"]["version"]), reverse=True)
        for version in response:
            if version["loader"]["stable"]:
                loader_version = version["loader"]["version"]
                break
        else:
            logger.critical("Could not find a compatible loader version.")
            return

    if installer_version == "latest":
        response = api.get(f"https://meta.fabricmc.net/v2/versions/installer")
        response = list(filter(lambda x: x["stable"] in (game_is_stable, True), response))
        # response.sort(key=lambda x: packaging.version.parse(x["version"]), reverse=True)
        for version in response:
            if version["stable"]:
                installer_version = version["version"]
                break
        else:
            logger.critical("Could not find a compatible installer version.")
            return

    logger.info(
        "Downloading Fabric %s for Minecraft %s with Installer version %s",
        loader_version,
        game_version,
        installer_version,
    )

    output_file = Path.cwd() / (
        f"fabric-server-mc.{game_version}-loader.{loader_version}-launcher." f"{installer_version}.jar"
    )
    if output_file.exists():
        rich.print("Fabric already downloaded.")
        return

    with api.http.stream(
        "GET",
        "https://meta.fabricmc.net/v2/versions/loader/%s/%s/%s/server/jar"
        % (game_version, loader_version, installer_version),
    ) as resp:
        with Progress(
            *Progress.get_default_columns(), DownloadColumn(os.name != "nt"), TransferSpeedColumn(), transient=True
        ) as progress:
            task = progress.add_task(
                "Downloading Fabric",
                total=int(resp.headers.get("content-length", 9999)),
                start="content-length" in resp.headers,
            )
            with open(output_file, "wb") as fd:
                for chunk in resp.iter_bytes():
                    fd.write(chunk)
                    progress.advance(task, len(chunk))
    rich.print("Downloaded Fabric to %s" % output_file)

    try:
        config, root = load_config()
    except RuntimeError:
        logger.warning("unable to update modman.json server version, please update manually or re-run init")
        return
    config["modman"]["root"] = str(Path.cwd())
    config["modman"]["server"]["type"] = "fabric"
    config["modman"]["server"]["version"] = game_version
    config["modman"]["server"]["file"] = str(output_file.resolve())
    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)
    rich.print("Updated modman.json server version. You should run `modman update` to update mods.")
    return output_file


@main.command("changelog")
@click.option("--verbose", "-V", is_flag=True, help="Whether to show extra release info.")
@click.option(
    "--sort-by",
    "-S",
    type=click.Choice(["date", "downloads", "changelog-size", "version-number"], case_sensitive=False),
    default="date",
    help="The field to sort by.",
)
@click.option(
    "--sort-direction",
    "-D",
    type=click.Choice(["asc", "desc"], case_sensitive=False),
    default="asc",
    help="The direction to sort. Asc is 0-9/A-Z, Desc is 9-0/Z-A.",
)
@click.option("--limit", "-L", type=int, default=10, help="The number of historical versions to show.")
@click.option(
    "--disable-hyperlinks",
    "-H",
    is_flag=True,
    help="Whether to disable hyperlinks in markdown rendering.",
    default=False,
)
@click.argument("mod", type=str, nargs=1, required=True)
@click.argument("version", type=str, nargs=1, required=False)
def see_changelog(
    mod: str,
    version: str | None,
    verbose: bool,
    sort_by: str,
    sort_direction: str,
    limit: int,
    disable_hyperlinks: bool,
):
    """Shows the changelog for a mod."""
    now = datetime.datetime.now(datetime.timezone.utc)

    def parse_release_date(v: dict) -> datetime.datetime:
        d = datetime.datetime.strptime(v["date_published"], "%Y-%m-%dT%H:%M:%S.%fZ")
        return d.replace(tzinfo=datetime.timezone.utc)

    def sort_by_changelog_size(v: dict) -> int:
        return len(v["changelog"])

    def sort_by_version_number(v: dict) -> str:
        return v["version_number"]

    def get_version_panel(v: dict) -> Panel:
        release_date = parse_release_date(v)
        match v["version_type"]:
            case "release":
                title_colour = "#1BD96A"
            case "beta":
                title_colour = "orange"
            case "alpha":
                title_colour = "red"
            case _:
                title_colour = "blue"

        if verbose:
            subtitle = [
                "Released " + good_time(release_date),
                "Downloads: {:,}".format(v["downloads"] or 0),
            ]
            subtitle = " | ".join(subtitle)
        else:
            subtitle = None
        md = Markdown(v["changelog"] or "*No changelog for this version.*", hyperlinks=not disable_hyperlinks)
        return Panel(
            md,
            title="[{1}]{0[id]} - {0[version_number]}[/]".format(v, title_colour),
            subtitle=subtitle,
        )

    sort_functions = {
        "date": lambda vs: parse_release_date(vs).timestamp(),
        "downloads": lambda vs: vs["downloads"],
        "changelog-size": sort_by_changelog_size,
        "version-number": sort_by_version_number,
    }

    def good_time(dt: datetime.datetime) -> str:
        if (now - dt).days > 365:
            return f"{round((now - dt).days / 365)} years ago"
        elif (now - dt).days > 30.5:
            return f"{round((now - dt).days / 30.5)} months ago"
        elif (now - dt).days > 7:
            return f"{round((now - dt).days / 7)} weeks ago"
        elif (now - dt).days > 0:
            return f"{(now - dt).days} days ago"
        return f"{round((now - dt).seconds / 3600)} hours ago"

    api = ModrinthAPI()
    config, root = load_config()
    mod_info = api.get_project(mod)
    if version is None:
        versions = api.get_versions(
            mod_info["id"],
            loader=config["modman"]["server"]["type"],
        )
        pages = []

        sorted_versions = list(sorted(versions, key=sort_functions[sort_by], reverse=sort_direction == "desc"))
        # filter out duplicate version numbers
        seen_version_numbers = set()
        for version in sorted_versions.copy():
            if version["version_number"] in seen_version_numbers:
                sorted_versions.remove(version)
                continue
            seen_version_numbers.add(version["version_number"])

        for version in reversed(sorted_versions[:limit]):
            panel = get_version_panel(version)
            pages.append(panel)
        for page in pages:
            rich.print(page)
            rich.print()
        return
    elif version == "latest":
        version_info = api.get_versions(
            mod_info["id"],
            loader=config["modman"]["server"]["type"],
        )[0]
    elif version in ["oldest", "first"]:
        version_info = api.get_versions(
            mod_info["id"],
            loader=config["modman"]["server"]["type"],
        )[-1]
    else:
        version_info = api.get_version(mod_info["id"], version)
    panel = get_version_panel(version_info)
    rich.print(panel)


@main.command("search")
@click.option(
    "--sort-by",
    "-S",
    type=click.Choice(["relevance", "downloads", "created", "updated"], case_sensitive=False),
    default="relevance",
    help="The field to sort by. Defaults ro relevance.",
)
@click.option("--page", "-P", type=int, default=1, help="The page to view. Evaluates to `--limit * (--page - 1)`.")
@click.option("--limit", "-L", type=int, default=20, help="The number of results to show.")
@click.argument("query", type=str, nargs=1, required=True)
def search(sort_by: str, page: int, limit: int, query: str):
    """Searches modrinth and returns a list of mods.

    QUERY is the query to search with. You may need to encapsulate it in "quotes" if it contains spaces.
    """
    if limit <= 0:
        logger.critical("Limit must be greater than 0.")
        return
    if limit > 100:
        logger.critical("Limit must not exceed 100.")
        return

    api = ModrinthAPI()
    try:
        config, root = load_config()
        logger.info(
            "Loaded modman config, using Minecraft version %s with server type %s.",
            config["modman"]["server"]["version"],
            config["modman"]["server"]["type"],
        )
    except RuntimeError:
        response = api.get("https://meta.fabricmc.net/v2/versions/game/intermediary")
        response = list(filter(lambda x: x["version"].count(".") == 2 and x["stable"], response))
        config = {"modman": {"server": {"type": "fabric", "version": response[0]["version"]}}, "mods": []}
        logger.info(
            "Failed to read modman config, using latest Minecraft version (%s) with fabric.",
            config["modman"]["server"]["version"],
        )
    offset = limit * (page - 1)

    results = api.search(
        query,
        limit,
        offset,
        sort_by,
        loaders=[config["modman"]["server"]["type"]],
        server_side=["required", "optional"],
        versions=[config["modman"]["server"]["version"]],
    )
    table = Table("Title", "ID", "Downloads", "Installed", "Description", title="Search Results")
    n = 0
    for result in results:
        table.add_row(
            ("[dim]%s[/]" if n % 2 else "[b]%s[/]") % result["title"],
            result["slug"],
            "{:,}".format(result["downloads"]),
            "\N{WHITE HEAVY CHECK MARK}" if result["slug"] in config["mods"] else "\N{CROSS MARK}",
            result["description"],
        )
        n += 1
    rich.print(table)


@main.command("view", aliases=["info", "show"])
@click.option("--no-hyperlinks", "-H", is_flag=True, help="Whether to disable hyperlinks in markdown rendering.")
@click.argument("mod", type=str, nargs=1, required=True)
def view(mod: str, no_hyperlinks: bool):
    """Views a mod's details."""
    api = ModrinthAPI()
    mod_info = api.get_project(mod)

    layout_master = Layout(name="master")
    layout_master.split(Layout(name="sidebar"), Layout(name="main"), splitter="row")
    layout_master["sidebar"].size = 60

    colours = {
        "approved": "green",
        "archived": "dim strike",
        "rejected": "red",
        "draft": "dim i",
        "unlisted": "grey",
        "processing": "cyan",
        "withheld": "yellow",
        "scheduled": "blue",
        "private": "dim",
        "unknown": "ul",
    }

    sidebar_content = [
        f"# {mod_info['title']}",
        f"{mod_info['description']}",
        "------------",
        "## Categories",
        ", ".join(mod_info["categories"]),
        "## Client support",
        f"* Server-side: {mod_info['server_side']}",
        f"* Client-side: {mod_info['client_side']}",
        "## Status",
        mod_info["status"],
        "## Downloads & Followers",
        "{:,} | {:,}".format(mod_info["downloads"], mod_info["followers"]),
        "## License",
        mod_info["license"]["name"],
        "## Supported versions",
        ", ".join(reversed(mod_info["game_versions"])),
        "## Links",
        "* Modrinth: https://modrinth.com/mod/%s" % mod_info["slug"],
    ]

    for k in ("issues_url", "source_url", "wiki_url", "discord_url"):
        if mod_info[k]:
            sidebar_content.append("* {}: {}".format(k.split("_")[0].capitalize(), mod_info[k]))

    layout_master["sidebar"].update(
        Panel(
            Markdown("\n".join(sidebar_content), hyperlinks=not no_hyperlinks),
            title="Mod Info",
        )
    )

    layout_master["main"].update(
        Panel(
            Markdown(mod_info["body"], hyperlinks=not no_hyperlinks),
            title="Description",
        )
    )

    rich.print(layout_master)


if __name__ == "__main__":
    main(auto_envvar_prefix="MODMAN")
