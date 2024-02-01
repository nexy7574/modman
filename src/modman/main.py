import zipfile

import httpx
import click
import rich
import json
import logging
from rich.logging import RichHandler
from rich.traceback import install
from pathlib import Path

from .lib import ModrinthAPI


def load_config():
    if not Path(".modman.json").exists():
        logging.critical("Could not find modman.json.")
        return

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
    logging.info("[green]Created modman.json.")


@main.command("install")
@click.argument("mods", type=str, nargs=-1)
@click.option(
    "--version",
    "-v",
    type=str,
    help="The version of the mod to install. Defaults to latest.",
    default="latest"
)
def install_mod(
        mods: tuple[str],
        version: str = "latest"
):
    """Installs a mod."""
    config = load_config()
    api = ModrinthAPI()
    collected_mods = []
    for mod in mods:
        mod_info = api.get_project(mod)
        if mod_info.get("server_side") == "unsupported":
            logging.warning("Mod %s is client-side only, you may not see an effect.", mod_info["title"])
        if version == "latest":
            versions = api.get_versions(
                mod_info["id"],
                loader=config["modman"]["server"]["type"],
                game_version=config["modman"]["server"]["version"]
            )
            version_info = versions[0]
        else:
            version_info = api.get_version(mod_info["id"], version["id"])
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

    for item in queue:
        _mod_info = item["mod"]
        _version_info = item["version"]
        api.download_mod(_version_info, Path.cwd() / "mods")
        config["mods"][_mod_info["slug"]] = {
            "project": _mod_info,
            "version": _version_info
        }
    rich.print("[green]Done.")
