import os
import zipfile

import httpx
import click
import rich
import json
import logging
import packaging.version
from rich.table import Table
from rich.logging import RichHandler
from rich.traceback import install
from pathlib import Path
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn

from .lib import ModrinthAPI


def load_config():
    if not Path(".modman.json").exists():
        logging.critical("Could not find modman.json. Have you run `modman init`?")
        raise click.Abort()

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
        handlers=[RichHandler(markup=True)]
    )


@main.command("init")
@click.option("--name", "-n", default=Path.cwd().name)
@click.option(
    "--auto/--no-auto", "-A/-N",
    default=True,
    help="Automatically detects installed mods."
)
@click.argument(
    "server_type",
    type=click.Choice(
        ["fabric", "forge", "auto"],
        case_sensitive=False
    ),
    default="auto"
)
@click.argument(
    "server_version",
    type=str,
    default="auto"
)
def init(
        name: str,
        auto: bool,
        server_type: str,
        server_version: str
):
    """Creates a modman project in the current directory."""
    if auto is False and (server_type == "auto" or server_version == "auto"):
        logging.error("'--no-auto' and '--server-type/-version auto' are mutually exclusive.")
        return

    config_data = {
        "modman": {
            "name": name,
            "server": {
                "type": server_type,
                "version": server_version
            }
        },
        "mods": {}
    }

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
                                    "Detected server version {!r}.".format(
                                        config_data['modman']['server']['version']
                                    )
                                )
                                changed = True
                        if not changed:
                            logging.info(
                                "Found install.properties, but could not determine server type."
                            )

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
                logging.info(
                    f"Detected {mod_info['title']!r} version {mod_version_info['name']!r} from {mod}"
                )
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
@click.option(
    "--optional/--no-optional",
    "-O/-N",
    default=False,
    help="Whether to install optional dependencies."
)
def install_mod(
        mods: tuple[str],
        optional: bool,
        reinstall: bool
):
    """Installs a mod."""
    config = load_config()
    api = ModrinthAPI()
    collected_mods = []
    for mod in mods:
        if "==" in mod:
            mod, version = mod.split("==")
        else:
            version = "latest"
        mod_info = api.get_project(mod)
        if mod_info["slug"] in config["mods"]:
            rich.print("Mod %s is already installed. Did you mean `modman update`?" % mod_info["title"])
            if reinstall is False:
                continue
            version = config["mods"][mod_info["slug"]]["version"]["id"]
        if mod_info.get("server_side") == "unsupported":
            logging.warning("Mod %s is client-side only, you may not see an effect.", mod_info["title"])

        if version == "latest":
            versions = api.get_versions(
                mod_info["id"],
                loader=config["modman"]["server"]["type"],
                game_version=config["modman"]["server"]["version"]
            )
            if not versions:
                logging.critical("Mod %s does not support %s (no versions).", mod_info["title"],
                                 config["modman"]["server"]["version"])
                continue
            version_info = versions[0]
        else:
            version_info = api.get_version(mod_info["id"], version)
        if config["modman"]["server"]["version"] not in version_info["game_versions"]:
            logging.warning(
                "Mod %s does not support %s, only %s.",
                mod_info["title"],
                config["modman"]["server"]["version"],
                ", ".join(version_info["game_versions"])
            )
            # continue
        if config["modman"]["server"]["type"] not in version_info["loaders"]:
            logging.warning(
                "Mod %s does not support %s.",
                mod_info["title"],
                config["modman"]["server"]["type"]
            )
            continue
        collected_mods.append(
            {
                "mod": mod_info,
                "version": version_info
            }
        )

    # Resolve dependencies
    queue = [
        *collected_mods
    ]
    for mod in collected_mods:
        mod_info = mod["mod"]
        version_info = mod["version"]
        for dependency_info in version_info["dependencies"]:
            if dependency_info["dependency_type"] == "optional" and not optional:
                logging.info(
                    "%s depends on %s==%s, but it is optional.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"]
                )
                continue
            logging.info(
                "%s depends on %s==%s",
                mod_info["title"],
                dependency_info["project_id"],
                dependency_info["version_id"]
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
                    "version": dependency_version
                }
            )

    table = Table("Mod", "Version", title="Installing Mods")
    for item in queue:
        _mod_info = item["mod"]
        _version_info = item["version"]
        logging.debug("Downloading %s==%s", _mod_info["title"], _version_info["name"])
        # rich.print(f"Downloading {_mod_info['title']} (version {_version_info['name']})")
        api.download_mod(_version_info, Path.cwd() / "mods")
        table.add_row(
            _mod_info["title"],
            _version_info["name"]
        )
        config["mods"][_mod_info["slug"]] = {
            "project": _mod_info,
            "version": _version_info
        }

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)
    rich.print(table)
    rich.print("[green]Done.")


@main.command("update")
@click.argument("mods", type=str, nargs=-1)
@click.option(
    "--game-version",
    "--server-version",
    "-V",
    type=str,
    default=None,
    help="The game version to update to."
)
@click.option(
    "--optional/--no-optional",
    "-O/-N",
    default=False,
    help="Whether to update optional dependencies."
)
def update_mod(
        mods: tuple[str],
        game_version: str = None,
        optional: bool = True
):
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
            mod_info["id"],
            loader=config["modman"]["server"]["type"],
            game_version=game_version
        )
        if not versions:
            logging.critical("Mod %s does not support %s (no versions).", mod_info["title"], game_version)
            continue
        version_info = versions[0]
        if version_info["id"] == config["mods"][mod_info["slug"]]["version"]["id"]:
            logging.info("Mod %s is already up to date.", mod_info["title"])
            continue
        to_update.append(
            {
                "mod": mod_info,
                "old_version": config["mods"][mod_info["slug"]]["version"],
                "new_version": version_info
            }
        )

    # Resolve dependencies
    queue = [
        *to_update
    ]
    for mod in to_update:
        mod_info = mod["mod"]
        version_info = mod["new_version"]
        for dependency_info in version_info["dependencies"]:
            if dependency_info["dependency_type"] == "optional" and not optional:
                logging.info(
                    "%s depends on %s==%s, but it is optional.",
                    mod_info["title"],
                    dependency_info["project_id"],
                    dependency_info["version_id"]
                )
                continue
            logging.info(
                "%s depends on %s==%s",
                mod_info["title"],
                dependency_info["project_id"],
                dependency_info["version_id"]
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
        config["mods"][_mod_info["slug"]] = {
            "project": _mod_info,
            "version": _version_info
        }

    with open(".modman.json", "w") as fd:
        json.dump(config, fd, indent=4)

    table = Table("Mod", "Old Version", "New Version", title="Updated Mods")
    for item in to_update:
        table.add_row(
            item["mod"]["title"],
            item["old_version"]["name"],
            item["new_version"]["name"]
        )
    rich.print(table)
    rich.print("[green]Done.")


@main.command("uninstall")
@click.argument("mods", type=str, nargs=-1)
@click.option(
    "--purge",
    "-P",
    is_flag=True,
    help="Whether to delete dependencies too."
)
def uninstall(mods: tuple[str], purge: bool):
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
    config = load_config()
    table = Table("Mod", "Version", title=f"Installed Mods (Minecraft {config['modman']['server']['version']})")
    for mod in config["mods"].values():
        file = Path.cwd() / "mods" / ModrinthAPI.pick_primary_file(mod["version"]["files"])["filename"]
        if not file.exists():
            logging.warning(
                "File %s does not exist. Was it deleted? Try `modman install -R %s`",
                file,
                mod["project"]["slug"]
            )
        table.add_row(
            mod["project"]["title"],
            mod["version"]["name"],
            style="" if file.exists() else "red"
        )
    rich.print(table)


@main.command("pack")
@click.option("--server-side", "-S", is_flag=True, help="Whether to include server-side mods.")
def create_pack(server_side: bool):
    """Creates a modpack zip to send to people using the server"""
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
        installer_version
    )

    output_file = Path.cwd() / (f"fabric-server-mc.{game_version}-loader.{loader_version}-launcher."
                                f"{installer_version}.jar")
    if output_file.exists():
        rich.print("Fabric already downloaded.")
        return

    with api.http.stream(
            "GET",
            "https://meta.fabricmc.net/v2/versions/loader/%s/%s/%s/server/jar" % (
                    game_version,
                    loader_version,
                    installer_version
            )
    ) as resp:
        with Progress(
            *Progress.get_default_columns(),
            DownloadColumn(os.name != "nt"),
            TransferSpeedColumn(),
            transient=True
        ) as progress:
            task = progress.add_task(
                "Downloading Fabric",
                total=int(resp.headers.get("content-length", 9999)),
                start="content-length" in resp.headers
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
