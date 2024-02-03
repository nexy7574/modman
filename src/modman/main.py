import datetime
import json
import logging
import os
import zipfile
from pathlib import Path

import click
import httpx
import packaging.version
import rich
from rich.layout import Layout
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn
from rich.table import Table
from rich.traceback import install

from .lib import ModrinthAPI

aikars_flags = [
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC",
    "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30",
    "-XX:G1MaxNewSizePercent=40",
    "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20",
    "-XX:G1HeapWastePercent=5",
    "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15",
    "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5",
    "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem",
    "-XX:MaxTenuringThreshold=1",
    "-Dusing.aikars.flags=https://mcflags.emc.gs",
    "-Daikars.new.flags=true",
]


def load_config():
    if not Path(".modman.json").exists():
        logging.warning("Could not find modman.json. Have you run `modman init`?")
        raise click.Abort("No modman.json found.")

    with open(".modman.json", "r") as fd:
        return json.load(fd)


@click.group("modman")
@click.option("--log-level", "-L", type=str, default="WARNING")
def main(log_level: str):
    if log_level.upper() == "DEBUG":
        install(show_locals=True)
    logging.basicConfig(
        level=logging.getLevelName(log_level.upper()),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(markup=True)],
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
        logging.error("Forge is not fully supported.")
    if auto is False and (server_type == "auto" or server_version == "auto"):
        logging.error("'--no-auto' and '--server-type/-version auto' are mutually exclusive.")
        return

    config_data = {"modman": {"name": name, "server": {"type": server_type, "version": server_version}}, "mods": {}}

    api = ModrinthAPI()

    if auto:
        for jar in Path.cwd().glob("*.jar"):
            with zipfile.ZipFile(jar) as _zip:
                if "install.properties" in _zip.namelist():
                    with _zip.open("install.properties") as fd:
                        install_properties = fd.read().decode("utf-8").splitlines()
                        changed = False
                        for line in install_properties:
                            if line.startswith("fabric-loader-version="):
                                config_data["modman"]["server"]["type"] = "fabric"
                                logging.info("Detected Fabric server.")
                                changed = True
                            elif line.startswith("game-version="):
                                config_data["modman"]["server"]["version"] = line.split("=")[1]
                                logging.info(
                                    "Detected server version {!r}.".format(config_data["modman"]["server"]["version"])
                                )
                                changed = True
                        if not changed:
                            logging.info("Found install.properties, but could not determine server type.")

        mods_dir = Path.cwd() / "mods"
        if not mods_dir.exists():
            logging.critical("'mods' directory does not exist. Cannot auto-detect mods.")
            return

        for mod in mods_dir.iterdir():
            if mod.is_dir():
                continue

            try:
                mod_version_info = api.get_version_from_hash(mod)
            except httpx.HTTPStatusError:
                logging.info(f"File {mod} does not appear to be a mod, or it is not on modrinth.")
                continue
            else:
                mod_info = api.get_project(mod_version_info["project_id"])
                logging.info(f"Detected {mod_info['title']!r} version {mod_version_info['name']!r} from {mod}")
                config_data["mods"][mod_info["slug"]] = {
                    "project": mod_info,
                    "version": mod_version_info,
                }

    with open(".modman.json", "w+") as fd:
        json.dump(config_data, fd, indent=4)
    rich.print("[green]Created modman.json.")


@main.command("install")
@click.argument("mods", type=str, nargs=-1)
@click.option("--reinstall", "-R", is_flag=True, help="Whether to reinstall already installed mods.")
@click.option("--optional/--no-optional", "-O/-N", default=False, help="Whether to install optional dependencies.")
def install_mod(mods: tuple[str], optional: bool, reinstall: bool):
    """Installs a mod."""
    config = load_config()
    if not mods:
        mods = config["mods"].keys()
        if not mods:
            logging.critical("No mods specified. Did you mean `modman init`?")
            return
    api = ModrinthAPI()
    collected_mods = []
    for mod in mods:
        if "==" in mod:
            mod, version = mod.split("==")
        else:
            version = "latest"
        mod_info = api.get_project(mod)
        if mod_info["slug"] in config["mods"]:
            logging.info("Mod %s is already installed. Did you mean `modman update`?" % mod_info["title"])
            if reinstall is False:
                continue
            version = config["mods"][mod_info["slug"]]["version"]["id"]
        if mod_info.get("server_side") == "unsupported":
            logging.warning("Mod %s is client-side only, you may not see an effect.", mod_info["title"])

        if version == "latest":
            versions = api.get_versions(
                mod_info["id"],
                loader=config["modman"]["server"]["type"],
                game_version=config["modman"]["server"]["version"],
            )
            if not versions:
                logging.critical(
                    "Mod %s does not support %s (no versions).",
                    mod_info["title"],
                    config["modman"]["server"]["version"],
                )
                continue
            version_info = versions[0]
        else:
            version_info = api.get_version(mod_info["id"], version)
        if config["modman"]["server"]["version"] not in version_info["game_versions"]:
            logging.warning(
                "Mod %s does not support %s, only %s.",
                mod_info["title"],
                config["modman"]["server"]["version"],
                ", ".join(version_info["game_versions"]),
            )
            # continue
        if config["modman"]["server"]["type"] not in version_info["loaders"]:
            logging.warning("Mod %s does not support %s.", mod_info["title"], config["modman"]["server"]["type"])
            continue
        collected_mods.append({"mod": mod_info, "version": version_info})

    # Resolve dependencies
    queue = [*collected_mods]
    for mod in collected_mods:
        mod_info = mod["mod"]
        version_info = mod["version"]
        for dependency_info in version_info["dependencies"]:
            if dependency_info["dependency_type"] == "optional" and not optional:
                logging.info(
                    "%s depends on %s==%s, but it is optional.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"],
                )
                continue
            logging.info(
                "%s depends on %s==%s", mod_info["title"], dependency_info["project_id"], dependency_info["version_id"]
            )
            dependency = api.get_project(dependency_info["project_id"])
            dependency_version = api.get_version(dependency_info["project_id"], dependency_info["version_id"])
            logging.info("Checking for dependency conflicts.")
            conflicts = api.find_dependency_conflicts(mod_info["id"], version_info["id"], config)
            if conflicts:
                logging.warning("Found dependency conflicts: %s", conflicts)
            queue.append({"mod": dependency, "version": dependency_version})

    # Resolve conflicts
    for item in queue.copy():
        mod_info = item["mod"]
        version_info = item["version"]

        for dependency in version_info["dependencies"]:
            if dependency["dependency_type"] == "incompatible":
                rich.print("[red]Mod %r is incompatible with %r." % (mod_info["title"], dependency["project_id"]))

    table = Table("Mod", "Version", title="Installing Mods")
    for item in queue:
        _mod_info = item["mod"]
        _version_info = item["version"]
        logging.debug("Downloading %s==%s", _mod_info["title"], _version_info["name"])
        # rich.print(f"Downloading {_mod_info['title']} (version {_version_info['name']})")
        api.download_mod(_version_info, Path.cwd() / "mods")
        table.add_row(_mod_info["title"], _version_info["name"])
        config["mods"][_mod_info["slug"]] = {"project": _mod_info, "version": _version_info}

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)
    rich.print(table)
    rich.print("[green]Done.")


@main.command("update")
@click.argument("mods", type=str, nargs=-1)
@click.option("--game-version", "--server-version", "-V", type=str, default=None, help="The game version to update to.")
@click.option("--optional/--no-optional", "-O/-N", default=False, help="Whether to update optional dependencies.")
def update_mod(mods: tuple[str], game_version: str = None, optional: bool = True):
    """Updates one or more mods.

    If no mods are specified, all mods will be updated.

    If a mod is specified, all of its dependencies will be updated too.

    If a mod does not have any updates available, it will be skipped.
    """
    api = ModrinthAPI()
    config = load_config()
    if not mods:
        logging.info("No mods specified. Updating all mods.")
        mods = config["mods"].keys()

    to_update = []
    for mod in mods:
        mod_info = api.get_project(mod)
        if mod_info["slug"] not in config["mods"]:
            logging.warning("Mod %s is not installed.", mod_info["title"])
            continue
        if game_version is None:
            game_version = config["modman"]["server"]["version"]
        versions = api.get_versions(
            mod_info["id"], loader=config["modman"]["server"]["type"], game_version=game_version
        )
        if not versions:
            logging.critical("Mod %s does not support %s (no versions).", mod_info["title"], game_version)
            continue
        version_info = versions[0]
        if version_info["id"] == config["mods"][mod_info["slug"]]["version"]["id"]:
            logging.info("Mod %s is already up to date.", mod_info["title"])
            continue
        to_update.append(
            {"mod": mod_info, "old_version": config["mods"][mod_info["slug"]]["version"], "new_version": version_info}
        )

    # Resolve dependencies
    queue = [*to_update]
    for mod in to_update:
        mod_info = mod["mod"]
        version_info = mod["new_version"]
        for dependency_info in version_info["dependencies"]:
            if dependency_info["dependency_type"] == "optional" and not optional:
                logging.info(
                    "%s depends on %s==%s, but it is optional.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"],
                )
                continue
            logging.info(
                "%s depends on %s==%s", mod_info["title"], dependency_info["project_id"], dependency_info["version_id"]
            )
            dependency = api.get_project(dependency_info["project_id"])
            dependency_version = api.get_version(dependency_info["project_id"], dependency_info["version_id"])
            logging.info("Checking for dependency conflicts.")
            conflicts = api.find_dependency_conflicts(mod_info["id"], version_info["id"], config)
            if conflicts:
                logging.warning("Found dependency conflicts: %s", conflicts)
            queue.append(
                {
                    "mod": dependency,
                    "old_version": dependency_version,
                    "new_version": dependency_version,
                }
            )

    for item in queue:
        _mod_info = item["mod"]
        _version_info = item["new_version"]
        if _version_info["id"] == item["old_version"]["id"]:
            logging.warning("Not upgrading %s, already up to date.", _mod_info["title"])
            continue
        logging.debug("Downloading %s==%s", _mod_info["title"], _version_info["name"])
        api.download_mod(_version_info, Path.cwd() / "mods")
        try:
            primary_file = ModrinthAPI.pick_primary_file(config["mods"][_mod_info["slug"]]["version"]["files"])
            (Path.cwd() / "mods" / primary_file["filename"]).unlink(True)
        except OSError as e:
            logging.warning("Could not remove old version: %s", e)
        config["mods"][_mod_info["slug"]] = {"project": _mod_info, "version": _version_info}

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)

    table = Table("Mod", "Old Version", "New Version", title="Updated Mods")
    for item in to_update:
        table.add_row(item["mod"]["title"], item["old_version"]["name"], item["new_version"]["name"])
    rich.print(table)
    rich.print("[green]Done.")


@main.command("uninstall")
@click.argument("mods", type=str, nargs=-1)
@click.option("--purge", "-P", is_flag=True, help="Whether to delete dependencies too.")
def uninstall(mods: tuple[str], purge: bool):
    """Properly deletes & uninstalls a mod."""
    if not mods:
        rich.print("[red]No mods specified.")
        return
    config = load_config()

    for mod in mods:
        if mod not in config["mods"]:
            rich.print(f"[red]Mod {mod} is not installed.")
            continue
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
                    (Path.cwd() / "mods" / primary_file["filename"]).unlink(True)
                    del config["mods"][dependency_info["project_id"]]
        rich.print(f"Uninstalling mod {mod_info['project']['title']}")
        primary_file = ModrinthAPI.pick_primary_file(mod_info["version"]["files"])
        (Path.cwd() / "mods" / primary_file["filename"]).unlink(True)
        del config["mods"][mod]

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)

    rich.print("[green]Done.")


@main.command("list")
def list_mods():
    """Lists all installed mods and their version."""
    config = load_config()
    table = Table("Mod", "Version", title=f"Installed Mods (Minecraft {config['modman']['server']['version']})")
    for mod in config["mods"].values():
        file = Path.cwd() / "mods" / ModrinthAPI.pick_primary_file(mod["version"]["files"])["filename"]
        if not file.exists():
            logging.warning(
                "File %s does not exist. Was it deleted? Try `modman install -R %s`", file, mod["project"]["slug"]
            )
        table.add_row(mod["project"]["title"], mod["version"]["name"], style="" if file.exists() else "red")
    rich.print(table)


@main.command("pack")
@click.option("--server-side", "-S", is_flag=True, help="Whether to include server-side mods.")
def create_pack(server_side: bool):
    """Creates a modpack zip to send to people using the server

    This will only include client-side mods by default. You can include all mods with the -S flag."""
    config = load_config()
    if not (Path.cwd() / "mods").exists():
        logging.critical("No mods directory found. Are you in the right directory?")
        return
    output_zip = Path.cwd() / (config["modman"]["name"] + ".zip")

    with zipfile.ZipFile(output_zip, "w", compresslevel=9) as _zip:
        for mod in config["mods"].values():
            if server_side is False and mod["project"]["client_side"] == "unsupported":
                logging.info("Skipping server-side mod %s", mod["project"]["title"])
                continue
            primary_file = ModrinthAPI.pick_primary_file(mod["version"]["files"])
            logging.info("Appending %s", primary_file["filename"])
            _zip.write(Path.cwd() / "mods" / primary_file["filename"], primary_file["filename"])
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
            logging.critical("Could not find a stable minecraft version.")
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
            logging.critical("Could not find a compatible loader version.")
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
            logging.critical("Could not find a compatible installer version.")
            return

    logging.info(
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
        config = load_config()
    except RuntimeError:
        logging.warning("unable to update modman.json server version, please update manually or re-run init")
        return
    config["modman"]["server"]["type"] = "fabric"
    config["modman"]["server"]["version"] = game_version
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
    config = load_config()
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
        logging.critical("Limit must be greater than 0.")
        return
    if limit > 100:
        logging.critical("Limit must not exceed 100.")
        return

    api = ModrinthAPI()
    try:
        config = load_config()
        logging.info(
            "Loaded modman config, using Minecraft version %s with server type %s.",
            config["modman"]["server"]["version"],
            config["modman"]["server"]["type"],
        )
    except RuntimeError:
        response = api.get("https://meta.fabricmc.net/v2/versions/game/intermediary")
        response = list(filter(lambda x: x["version"].count(".") == 2 and x["stable"], response))
        config = {"modman": {"server": {"type": "fabric", "version": response[0]["version"]}}, "mods": []}
        logging.info(
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
            "\N{white heavy check mark}" if result["slug"] in config["mods"] else "\N{cross mark}",
            result["description"],
        )
        n += 1
    rich.print(table)


@main.command("view")
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
