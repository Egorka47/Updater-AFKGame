import os
import sys

APP_NAME = "XyesosBeta"

def exe_dir() -> str:
    # папка где лежит exe (или .py при разработке)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def res_dir() -> str:
    # папка ресурсов (в onefile это _MEIPASS)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def get_app_data_dir(app_name: str = APP_NAME) -> str:
    # папка куда можно писать без прав админа
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")).strip()
    d = os.path.join(base, app_name)
    os.makedirs(d, exist_ok=True)
    return d