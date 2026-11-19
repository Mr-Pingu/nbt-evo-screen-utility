# NBT EVO Screen Utility

A lightweight screen-streaming utility for a BMW NBT EVO head unit. It checks SSH on the head unit before connecting, uploads the QNX screen helper, and displays the live stream in a minimal window. The utility runs on Windows, Linux, and macOS; the helper binary runs on the QNX head unit.

Download the ZIP for your desktop from the [v1.0.0 release](https://github.com/Mr-Pingu/nbt-evo-screen-utility/releases/tag/v1.0.0). The platform ZIPs include the compiled head-unit helper. If you download the source, build the helper before streaming.

## Requirements

- Python 3.9 or newer on Windows, Linux, or macOS.
- Tkinter support in the Python installation (usually included; Linux may require `python3-tk`).
- A powered and awake head unit connected over Ethernet, accepting SSH connections on port 22.
- SSH access as `root` (or another account allowed to run the helper and manage the `filter_OBD` PF anchor).
- SSH host keys are checked. On first connection to an unknown key, the utility shows its SHA-256 fingerprint and asks whether to add it to `~/.ssh/known_hosts`.
- `native/hu-screen-mirror`, built for the head unit with the QNX 6.5 ARMv7 SDK.
- Internet access on first launch so the launcher can install the Python requirements into a local virtual environment.

Install the Python dependencies:

```sh
python -m pip install -r requirements.txt
```

## Usage

From a release ZIP, extract the folder and start with the platform launcher. It uses default settings unless you pass optional command-line arguments:

```text
Windows: double-click run_windows.bat
Linux/macOS: ./run.sh
```

The default head-unit address is `169.254.199.119`. If SSH port 22 is not open, the utility retries twice and then prints `aborted` without prompting or attempting a stream. Otherwise, it always prompts for the SSH password; key and agent authentication are not used. If the SSH host key is not yet trusted, the utility displays its fingerprint and asks whether to save it to `~/.ssh/known_hosts`. Confirm the fingerprint with the HU administrator before accepting it.

For development, run directly from the project directory with `python hu_stream.py`.

Examples:

```sh
python hu_stream.py --host 192.168.1.25
python hu_stream.py --host 192.168.1.25 --username root --resolution 1280x480
python hu_stream.py --resolution 900x360 --fps 30
```

The window contains only the live image. Close it or press **Escape** to stop the stream; the temporary HU firewall rule is removed during normal shutdown.

### Command-line parameters

| Parameter | Default | Description |
| --- | --- | --- |
| `--host IPv4` | `169.254.199.119` | Head-unit IPv4 address. |
| `--username NAME` | `root` | SSH account on the head unit. |
| `--resolution WIDTHxHEIGHT` | `900x360` | Stream window size. Width must be 320–1280 and height 180–2160. The helper captures at the requested width and preserves the HU display's aspect ratio; the displayed image is resized to the exact requested dimensions. |
| `--fps N` | `30` | Capture rate, from 1 to 60 frames per second. |
| `-h`, `--help` | — | Show all options and exit. |

Use the standard `python` command appropriate to your installation (for example `python3` on some Linux/macOS systems).

## Running the tests

From the project root, run `python -m unittest discover -s tests -v`.

## Building the QNX helper

The `QNX_SDK_PATH` environment variable must point to the root of the installed QNX 6.5 SDK. The helper is compiled for the HU, not for the desktop OS. Build it on Windows with PowerShell:

```powershell
$env:QNX_SDK_PATH = 'C:\QNX650'
.\native\build.ps1
```

Or pass the SDK path directly:

```powershell
.\native\build.ps1 -SdkPath 'C:\QNX650'
```

On Linux or macOS:

```sh
export QNX_SDK_PATH=/opt/qnx650
sh native/build.sh
```

The build produces `native/hu-screen-mirror`. The SDK must include its host compiler (`ntoarmv7-gcc`) and QNX 6.5 target files. macOS builds require a QNX SDK installation that includes the Darwin-hosted ARMv7 compiler.

## Creating and running release ZIPs

Build `native/hu-screen-mirror` once with the QNX 6.5 SDK as described above, then create all three downloadable ZIPs:

```sh
python scripts/package.py --platform all
```

The versioned ZIPs (`nbt-evo-screen-utility-v1.0.0-<platform>.zip`) are written to `dist/`. To create just one, use `--platform windows`, `--platform linux`, or `--platform macos`. Each ZIP contains the Python utility, its icon, platform launcher, and the already-compiled QNX helper. Users do not need the QNX SDK. The helper is for the same HU/QNX target in all three packages; only the desktop launcher differs.

The launchers check for Python 3.9+, Tkinter, and the required Python modules. They create a local `.venv`, automatically download/install any missing requirements from `requirements.txt`, then start with default settings. Internet access is needed when dependencies must be downloaded. Clear errors are shown if Python/Tkinter is unavailable or dependency installation fails. Windows users double-click `run_windows.bat`; Linux/macOS users run `./run.sh` from a terminal. Optional streamer arguments can be added after the launcher, such as `./run.sh --host 192.168.1.25`.

## Security and troubleshooting

- SSH host keys are checked against known-hosts files. A new key requires confirmation and is saved to the user's `~/.ssh/known_hosts`; a changed, previously trusted key is not replaced automatically.
- The temporary screen rule is limited to this computer, the HU address, and TCP port 5555. The stream payload itself is not encrypted; use an isolated bench network.
- If SSH is open but authentication fails, verify the SSH username and password.
- If startup reports a missing native helper, build it as described above.
- If the stream cannot start, confirm the HU is awake, SSH works, and the QNX helper matches the HU's architecture.

## License and credits

The project is distributed under the [MIT License](LICENSE). The original iDrive controller assets and protocol references were copied from the upstream project credited in the license; this screen-streaming CLI does not use or require CAN hardware.
