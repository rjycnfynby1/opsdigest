import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def install_fake_telethon():
    """Минимальная подмена telethon для тестов без сети (если настоящий не установлен)."""
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass
    tl = types.ModuleType("telethon")
    utils = types.ModuleType("telethon.utils")

    def get_display_name(e):
        if getattr(e, "title", None):
            return e.title
        return " ".join(x for x in (getattr(e, "first_name", ""), getattr(e, "last_name", "")) if x)

    utils.get_display_name = get_display_name
    conn = types.ModuleType("telethon.connection")
    for n in ("ConnectionTcpMTProxyIntermediate", "ConnectionTcpMTProxyRandomizedIntermediate",
              "ConnectionTcpMTProxyAbridged"):
        setattr(conn, n, type(n, (), {}))
    tl.utils, tl.connection = utils, conn
    sys.modules["telethon"], sys.modules["telethon.utils"], sys.modules["telethon.connection"] = tl, utils, conn


install_fake_telethon()
