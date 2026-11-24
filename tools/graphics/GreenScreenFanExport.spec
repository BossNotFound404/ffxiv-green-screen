# PyInstaller spec for the optional Windows exporter shipped beside the plugin.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

here = Path(SPECPATH)
data = [(str(here / "fan_sequence_import.jsx"), "."), (str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/python")]
for dependency in ("numpy", "OpenEXR", "Pillow"):
    data += copy_metadata(dependency)
from importlib.metadata import distribution
boot_license = distribution("pyinstaller").locate_file("pyinstaller-" + distribution("pyinstaller").version + ".dist-info/licenses/COPYING.txt")
data.append((str(boot_license), "licenses/pyinstaller"))

a = Analysis([str(here / "export_fan_ae.py")], pathex=[str(here)],
             datas=data, hiddenimports=["Imath"], excludes=["tkinter", "matplotlib", "pytest", "scipy"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GreenScreenFanExport", console=True, upx=False)
collect = COLLECT(exe, a.binaries, a.datas, name="FanExport", upx=False)
