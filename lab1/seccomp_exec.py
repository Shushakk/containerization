#!/usr/bin/python3

import json
import os
import sys
import seccomp


def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "Usage: seccomp_exec.py PROFILE COMMAND [ARGS...]"
        )

    profile_path = sys.argv[1]
    command = sys.argv[2]
    args = sys.argv[2:]

    with open(profile_path, encoding="utf-8") as file:
        profile = json.load(file)

    if profile.get("default_action") != "allow":
        raise SystemExit("Unsupported default action")

    flt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)

    for rule in profile.get("rules", []):
        if rule.get("action") != "errno":
            raise SystemExit("Unsupported seccomp action")

        action = seccomp.ERRNO(int(rule.get("errno", 1)))

        for syscall in rule["syscalls"]:
            flt.add_rule(action, syscall)

    flt.load()
    os.execv(command, args)


if __name__ == "__main__":
    main()
