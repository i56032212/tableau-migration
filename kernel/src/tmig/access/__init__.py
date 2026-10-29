"""存取控制：使用者目錄（第三階段 P2）與權限（P3）。

- ``directory``：Tableau Server 上的使用者與群組；登入的帳號依名稱對應，決定 USERNAME()、ISMEMBEROF() 的值
- ``permissions``：Project、workbook、view 的權限，依 Tableau 的規則判斷登入者看不看得到
"""

from tmig.access.directory import (
    Directory,
    DirectoryError,
    DirectoryUser,
    load_directory,
    login_name,
)
from tmig.access.permissions import Access, Permissions, PermissionsError, load_permissions

__all__ = [
    "Access",
    "Directory",
    "DirectoryError",
    "DirectoryUser",
    "Permissions",
    "PermissionsError",
    "load_directory",
    "load_permissions",
    "login_name",
]
