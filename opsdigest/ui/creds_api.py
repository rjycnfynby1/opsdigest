"""«Учётные данные» в окне: список (без секретов), добавить/изменить/удалить, какому плагину какая запись,
показать/скопировать секрет — только после повторного ввода пароля OpsDigest (secure/creds.py)."""
from __future__ import annotations

from ..secure import creds as C
from ..secure import vault


def route(h, method: str, path: str, b: dict, q: dict):
    from .server import UserError
    app = h.app
    ws = app.need_ws()
    try:
        if path == "/api/creds":
            return C.listing(ws)
        if method != "POST":
            return None
        if path == "/api/creds/save":
            return {"item": C.save(ws, b.get("item") or {}), **C.listing(ws)}
        if path == "/api/creds/delete":
            C.delete(ws, str(b.get("id") or ""))
            return C.listing(ws)
        if path == "/api/creds/assign":
            C.assign(ws, str(b.get("plugin") or ""), str(b.get("id") or "") or None)
            return C.listing(ws)
        if path == "/api/creds/open":
            return {"open_left": C.open_with_password(str(b.get("password") or ""))}
        if path == "/api/creds/close":
            C.close()
            return {"open_left": 0}
        if path == "/api/creds/reveal":
            return C.reveal(ws, str(b.get("id") or ""), str(b.get("field") or "secret"), b.get("password") or None)
    except C.NeedPassword as e:
        raise UserError(str(e)) from None
    except vault.BadSecret as e:
        raise UserError(str(e)) from None
    except ValueError as e:
        raise UserError(str(e)) from None
    return None
