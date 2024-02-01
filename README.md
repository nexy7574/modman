# ModMan

A package-manager like mod manager for minecraft servers, integrated with Modrinth.

## Installation 

Use `pip` or `pipx`, via git, to install ModMan.
```bash
pip install git+https://github.com/nexy7574/modman.git
```

And then run `modman --help` to get started.


## Usage

Note that you can specify multiple mods at once, separated by spaces.

### Initial setup

You will first want to go to your server's directory, and run `modman init`.
This will create a `modman.json` file, which will be used to store your mod list.

If you had a `mods` directory in your cwd, modman will automatically add all the detected mods.

It will also attempt to automatically detect your server type (fabric only) and version from and jars in your cwd.

### Adding mods

You simply just need to run `modman install <mod slug or ID>`. ModMan will automatically find the latest compatible
version, and perform basic dependency resolution, and then securely download them into your mods directory.

If a file is missing, you can run `modman install -R <mod slug or ID>` to forcefully re-install the missing mod.

You can install optional dependencies with the `-O` flag.

### Removing mods

Run `modman uninstall <mod slug or ID>`. This will remove the mod from your mod list, and delete the mod file.

### Updating mods

#### Same game version

If you are keeping the same game version (e.g. 1.20.4), you can run `modman update` to update all mods to the latest
version.

#### Different game version

You will first need to download the latest server and update the metadata file. The recommended way to do this is via
`modman download-fabric <minecraft version>`. This will download the latest server jar, and update the metadata file
for you.

You can then run `modman update` to update all mods to the latest version.

Any mods that do not support the current version will not be modified.

### Listing installed mods

`modman list` produces a nice table of installed mods and their versions.

### Creating client mod packs

ModMan allows you to pack all mods that have client-side support into a zip file to share with friends or server
members.

Running `modman pack` will create a zip file in the current directory, containing all mods that have client-side
support.

If you want to also include mods that are server-side only for some reason, you can use the `-S` flag.

### Debugging

You can pass `-L DEBUG` to `modman` (e.g. `modman -L DEBUG install fabric-api) to get VERY verbose output.

You can pass `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` to `modman` to get different levels of verbosity.

**You cannot pass this flag to subcommands, it has to be BEFORE a subcommand.**
