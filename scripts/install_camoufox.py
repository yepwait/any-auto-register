import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from platformdirs import user_cache_dir


def main() -> None:
    version = os.environ["CAMOUFOX_VERSION"]
    release = os.environ["CAMOUFOX_RELEASE"]
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "i686",
        "i686": "i686",
        "x86": "i686",
    }
    machine = os.uname().machine.lower()
    arch = arch_map.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Camoufox arch: {machine}")

    tag = f"v{version}-{release}"
    asset_name = f"camoufox-{version}-{release}-lin.{arch}.zip"
    asset_url = f"https://github.com/daijro/camoufox/releases/download/{tag}/{asset_name}"
    addon_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
    install_dir = Path(user_cache_dir("camoufox"))
    temp_dir = Path(tempfile.mkdtemp(prefix="camoufox-install-"))

    try:
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)

        # Camoufox 0.5+ only activates browsers registered under this versioned path.
        version_key = f"{version}-{release}"
        version_dir = install_dir / "browsers" / "official" / version_key
        version_dir.mkdir(parents=True, exist_ok=True)

        archive_path = temp_dir / asset_name
        print(f"Downloading Camoufox package: {asset_url}")
        urllib.request.urlretrieve(asset_url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(version_dir)

        version_path = version_dir / "version.json"
        version_path.write_text(
            json.dumps({"version": version, "release": release}),
            encoding="utf-8",
        )
        (install_dir / "config.json").write_text(
            json.dumps({"active_version": f"browsers/official/{version_key}"}),
            encoding="utf-8",
        )
        # Camoufox 0.5+ uses this marker to keep the preloaded browser install.
        (install_dir / ".0.5_FLAG").touch()

        addon_dir = version_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True, exist_ok=True)
        addon_path = temp_dir / "ublock-origin.xpi"
        print(f"Downloading default addon UBO: {addon_url}")
        try:
            request = urllib.request.Request(
                addon_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Camoufox installer)"},
            )
            with urllib.request.urlopen(request, timeout=60) as response, addon_path.open("wb") as output:
                shutil.copyfileobj(response, output)
            with zipfile.ZipFile(addon_path) as zf:
                zf.extractall(addon_dir)
        except Exception as exc:  # noqa: BLE001 - UBO is optional and may be blocked upstream
            print(f"Warning: skipped optional UBO addon: {type(exc).__name__}: {exc}")

        for path in install_dir.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

        binary = version_dir / "camoufox-bin"
        if binary.exists():
            binary.chmod(0o755)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
