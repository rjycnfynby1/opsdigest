"""Защита данных на диске: пароль, ПИН, шифрование базы, вложений, отчётов, настроек и сессии Telegram.

vault  — ключ, пароль, код восстановления, ПИН, блокировка;
files  — чтение/запись файлов рабочей папки (в защищённой папке — зашифрованных);
memdb  — база SQLite в памяти, на диске — зашифрованный снимок;
crypto — шифр на стандартной библиотеке Python.
"""
from . import crypto, files, memdb, vault
from .vault import BadSecret, Locked

__all__ = ["crypto", "files", "memdb", "vault", "Locked", "BadSecret"]
