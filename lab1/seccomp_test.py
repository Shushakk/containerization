#!/usr/bin/python3

import errno
import os
import seccomp

# Разрешаем системные вызовы по умолчанию.
flt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)

# Запрещаем uname и возвращаем EPERM.
flt.add_rule(seccomp.ERRNO(errno.EPERM), "uname")

# Применяем фильтр к текущему процессу.
flt.load()

# Запускаем uname: фильтр сохранится после exec.
os.execv("/usr/bin/uname", ["uname", "-a"])
