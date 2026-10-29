"""存取控制：使用者目錄（第三階段 P2）與權限（P3）。

- ``directory``：Tableau Server 上的使用者與群組；登入的帳號依名稱對應，決定 USERNAME()、ISMEMBEROF() 的值
"""

from tmig.access.directory import (
    Directory,
    DirectoryError,
    DirectoryUser,
    load_directory,
    login_name,
)

__all__ = ["Directory", "DirectoryError", "DirectoryUser", "load_directory", "login_name"]
