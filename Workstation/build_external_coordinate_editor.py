from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


APP_NAME = "external_coordinate_editor"
SOURCE_FILES = ("app.py", "standalone_data.py", "standalone_utils.py")
RUNTIME_DLLS = ("tcl86t.dll", "tk86t.dll", "zlib1.dll")
REQUIRED_PACKAGE_FILES = (
    "external_coordinate_editor.exe",
    "_internal/_tkinter.pyd",
    "_internal/tcl86t.dll",
    "_internal/tk86t.dll",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the external coordinate editor executable.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist") / APP_NAME,
        help="Output folder for the PyInstaller onedir package.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python executable that has PyInstaller and Pillow installed.",
    )
    parser.add_argument(
        "--include-export",
        type=Path,
        help="Optional coordinate_editor_standalone_export_* folder to copy beside the exe.",
    )
    parser.add_argument("--force", action="store_true", help="Delete the output folder before building.")
    parser.add_argument("--console", action="store_true", help="Build a console exe for debugging.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary build files.")
    return parser.parse_args()


def copy_sources(source_dir: Path, temp_source_dir: Path) -> None:
    temp_source_dir.mkdir(parents=True, exist_ok=True)
    for filename in SOURCE_FILES:
        shutil.copy2(source_dir / filename, temp_source_dir / filename)


def run_pyinstaller(python: Path, temp_source_dir: Path, temp_build_dir: Path, console: bool) -> Path:
    window_mode = "--console" if console else "--windowed"
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        window_mode,
        "--name",
        APP_NAME,
        "--distpath",
        str(temp_build_dir / "dist"),
        "--workpath",
        str(temp_build_dir / "build"),
        "--specpath",
        str(temp_build_dir),
        "--paths",
        str(temp_source_dir),
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "cv2",
        "--hidden-import",
        "standalone_data",
        "--hidden-import",
        "standalone_utils",
        "--onedir",
        str(temp_source_dir / "app.py"),
    ]
    subprocess.run(command, check=True)
    return temp_build_dir / "dist" / APP_NAME


def copy_runtime_dlls(python: Path, output_dir: Path) -> None:
    env_root = python.resolve().parent
    runtime_source_dir = env_root / "Library" / "bin"
    internal_dir = output_dir / "_internal"

    missing = []
    for filename in RUNTIME_DLLS:
        source = runtime_source_dir / filename
        if not source.is_file():
            missing.append(str(source))
            continue
        shutil.copy2(source, internal_dir / filename)

    if missing:
        raise FileNotFoundError("Missing runtime DLLs:\n" + "\n".join(missing))


def copy_optional_export(export_dir: Path | None, output_dir: Path) -> None:
    if export_dir is None:
        return

    export_dir = export_dir.resolve()
    if not export_dir.is_dir():
        raise FileNotFoundError(f"Export folder not found: {export_dir}")
    if not export_dir.name.startswith("coordinate_editor_standalone_export_"):
        raise ValueError(f"Export folder name is not supported: {export_dir.name}")

    shutil.copytree(export_dir, output_dir / export_dir.name)


def validate_package(output_dir: Path) -> dict[str, int]:
    missing = [
        relative_path
        for relative_path in REQUIRED_PACKAGE_FILES
        if not (output_dir / relative_path).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Built package is missing required PyInstaller files:\n" + "\n".join(missing)
        )

    files = [path for path in output_dir.rglob("*") if path.is_file()]
    pyd_files = [path for path in files if path.suffix.lower() == ".pyd"]
    if not pyd_files:
        raise FileNotFoundError("Built package contains no .pyd native extension modules.")

    return {
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "pyd_count": len(pyd_files),
    }


def main() -> int:
    args = parse_args()
    project_dir = Path(__file__).resolve().parent
    source_dir = project_dir / "coordinate_editor_standalone"
    output_dir = (project_dir / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    dist_dir = project_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    work_parent = Path(os.environ.get("TEMP", str(dist_dir)))

    if output_dir.exists():
        if not args.force:
            print(f"Output folder already exists: {output_dir}")
            print("Use --force to replace it.")
            return 2
        shutil.rmtree(output_dir)

    temp_root = work_parent / f"{APP_NAME}_build_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    suffix = 1
    while temp_root.exists():
        temp_root = dist_dir / f"{APP_NAME}_build_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}"
        suffix += 1
    temp_root.mkdir(parents=True)
    try:
        temp_source_dir = temp_root / "src"
        temp_build_dir = temp_root / "pyinstaller"

        copy_sources(source_dir, temp_source_dir)
        built_dir = run_pyinstaller(args.python, temp_source_dir, temp_build_dir, args.console)
        shutil.copytree(built_dir, output_dir)
        copy_runtime_dlls(args.python, output_dir)
        copy_optional_export(args.include_export, output_dir)
        package_stats = validate_package(output_dir)

        if args.keep_temp:
            print(f"Kept temporary build files: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)
    except Exception:
        print(f"Temporary build files were kept for inspection: {temp_root}")
        raise

    print(f"Built: {output_dir}")
    print(f"Files: {package_stats['file_count']}")
    print(f"Native .pyd files: {package_stats['pyd_count']}")
    print(f"Total size: {package_stats['total_bytes']:,} bytes ({package_stats['total_bytes'] / (1024 ** 2):.2f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
