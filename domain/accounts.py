from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ACCOUNT_HEADERS = ("code", "password")
CODE_FIELDS = ("code", "username", "account")
PASSWORD_FIELDS = ("password", "passwd", "pwd")
MAX_ACCOUNTS_FILE_BYTES = 256 * 1024
MAX_ACCOUNT_LENGTH = 128
MAX_PASSWORD_LENGTH = 512
MAX_LIST_DISPLAY_LINES = 80


@dataclass(frozen=True, slots=True)
class EktaAccount:
    code: str
    password: str


@dataclass(frozen=True, slots=True)
class AccountMutationResult:
    account: str
    count: int
    existed: bool


@dataclass(frozen=True, slots=True)
class AccountListResult:
    accounts: tuple[EktaAccount, ...]
    total: int
    display_limit: int

    @property
    def omitted(self) -> int:
        return max(0, self.total - len(self.accounts))


@dataclass(slots=True)
class _AccountSheet:
    headers: list[str]
    rows: list[dict[str, str]]
    code_field: str
    password_field: str


class AccountCsvError(RuntimeError):
    pass


class CsvAccountStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def add(self, account: str, password: str) -> AccountMutationResult:
        account = _validate_field(
            account,
            field_name="账号",
            max_length=MAX_ACCOUNT_LENGTH,
        )
        password = _validate_field(
            password,
            field_name="密码",
            max_length=MAX_PASSWORD_LENGTH,
        )
        async with self._lock:
            return await asyncio.to_thread(self._add_sync, account, password)

    async def delete(self, account: str) -> AccountMutationResult:
        account = _validate_field(
            account,
            field_name="账号",
            max_length=MAX_ACCOUNT_LENGTH,
        )
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, account)

    async def list_accounts(self) -> AccountListResult:
        async with self._lock:
            return await asyncio.to_thread(self._list_sync)

    def _add_sync(self, account: str, password: str) -> AccountMutationResult:
        sheet = self._load_sheet()
        existed = False
        for row in sheet.rows:
            if row.get(sheet.code_field, "") == account:
                row[sheet.password_field] = password
                existed = True
                break
        if not existed:
            row = {header: "" for header in sheet.headers}
            row[sheet.code_field] = account
            row[sheet.password_field] = password
            sheet.rows.append(row)
        self._write_sheet(sheet)
        return AccountMutationResult(account=account, count=len(sheet.rows), existed=existed)

    def _delete_sync(self, account: str) -> AccountMutationResult:
        sheet = self._load_sheet()
        original_count = len(sheet.rows)
        sheet.rows = [
            row for row in sheet.rows if row.get(sheet.code_field, "") != account
        ]
        existed = len(sheet.rows) != original_count
        if existed:
            self._write_sheet(sheet)
        return AccountMutationResult(account=account, count=len(sheet.rows), existed=existed)

    def _list_sync(self) -> AccountListResult:
        sheet = self._load_sheet()
        accounts = tuple(
            EktaAccount(
                code=row.get(sheet.code_field, ""),
                password=row.get(sheet.password_field, ""),
            )
            for row in sheet.rows[:MAX_LIST_DISPLAY_LINES]
        )
        return AccountListResult(
            accounts=accounts,
            total=len(sheet.rows),
            display_limit=MAX_LIST_DISPLAY_LINES,
        )

    def _load_sheet(self) -> _AccountSheet:
        if not self._path.exists():
            headers = list(DEFAULT_ACCOUNT_HEADERS)
            return _AccountSheet(
                headers=headers,
                rows=[],
                code_field=headers[0],
                password_field=headers[1],
            )
        if not self._path.is_file():
            raise AccountCsvError("账号 CSV 路径不是文件。")
        try:
            size = self._path.stat().st_size
        except OSError as exc:
            raise AccountCsvError("账号 CSV 状态不可读。") from exc
        if size > MAX_ACCOUNTS_FILE_BYTES:
            raise AccountCsvError("账号 CSV 文件过大，已拒绝在聊天命令中修改。")

        try:
            with self._path.open("r", encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                headers = [str(header).strip() for header in (reader.fieldnames or [])]
                if not headers:
                    headers = list(DEFAULT_ACCOUNT_HEADERS)
                    rows: list[dict[str, str]] = []
                else:
                    rows = [
                        {
                            header: _cell_text(row.get(header, ""))
                            for header in headers
                        }
                        for row in reader
                    ]
        except csv.Error as exc:
            raise AccountCsvError("账号 CSV 格式不正确。") from exc
        except OSError as exc:
            raise AccountCsvError("账号 CSV 不可读。") from exc

        code_field = _first_existing(headers, CODE_FIELDS)
        password_field = _first_existing(headers, PASSWORD_FIELDS)
        if code_field is None or password_field is None:
            raise AccountCsvError("账号 CSV 必须包含 code,password 表头。")
        return _AccountSheet(
            headers=headers,
            rows=[
                row for row in rows if any(value.strip() for value in row.values())
            ],
            code_field=code_field,
            password_field=password_field,
        )

    def _write_sheet(self, sheet: _AccountSheet) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AccountCsvError("账号 CSV 目录不可写。") from exc

        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_name = file.name
                writer = csv.DictWriter(
                    file,
                    fieldnames=sheet.headers,
                    extrasaction="ignore",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(sheet.rows)
            os.replace(temp_name, self._path)
            _restrict_owner_access(self._path)
        except OSError as exc:
            raise AccountCsvError("账号 CSV 写入失败。") from exc
        finally:
            if temp_name:
                try:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass


def _validate_field(value: str, *, field_name: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AccountCsvError(f"{field_name}不能为空。")
    if len(text) > max_length:
        raise AccountCsvError(f"{field_name}长度不能超过 {max_length} 个字符。")
    if any(char in text for char in ("\r", "\n")):
        raise AccountCsvError(f"{field_name}不能包含换行。")
    return text


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_existing(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {header.casefold(): header for header in headers}
    for candidate in candidates:
        header = normalized.get(candidate)
        if header is not None:
            return header
    return None


def _restrict_owner_access(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
