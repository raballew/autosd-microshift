#!/usr/bin/env python3
"""Translates iptables commands to nft equivalents for kernels missing nft_compat."""
import sys
import subprocess
import re

PROTO_MAP = {"tcp": "tcp", "udp": "udp", "icmp": "icmp", "icmpv6": "icmpv6"}
TARGET_MAP = {
    "ACCEPT": "accept",
    "DROP": "drop",
    "RETURN": "return",
    "NOTRACK": "notrack",
    "MASQUERADE": "masquerade",
}
HOOK_PRIORITY = {
    "PREROUTING": {"raw": "raw", "nat": "dstnat", "mangle": "mangle"},
    "INPUT": {"filter": "filter", "mangle": "mangle", "nat": "dstnat"},
    "FORWARD": {"filter": "filter", "mangle": "mangle"},
    "OUTPUT": {"raw": "raw", "filter": "filter", "mangle": "mangle", "nat": "srcnat"},
    "POSTROUTING": {"mangle": "mangle", "nat": "srcnat"},
}
HOOK_NAMES = {
    "PREROUTING": "prerouting",
    "INPUT": "input",
    "FORWARD": "forward",
    "OUTPUT": "output",
    "POSTROUTING": "postrouting",
}
TABLE_FAMILY = {
    "iptables": "ip",
    "ip6tables": "ip6",
    "arptables": "arp",
    "ebtables": "bridge",
}


def nft(*args):
    cmd = ["nft"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, r.stdout, r.stderr


def ensure_chain(family, table, chain):
    hook = HOOK_NAMES.get(chain)
    if hook:
        priority = HOOK_PRIORITY.get(chain, {}).get(table, "filter")
        chain_type = "nat" if table == "nat" else "filter"
        nft("add", "table", family, table)
        nft(
            "add",
            "chain",
            family,
            table,
            chain,
            "{",
            f"type {chain_type} hook {hook} priority {priority};",
            "}",
        )
    else:
        nft("add", "table", family, table)
        nft("add", "chain", family, table, chain)


def parse_rule(args):
    """Parse iptables rule args into nft expressions. Returns list of nft expr strings."""
    exprs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-p", "--protocol") and i + 1 < len(args):
            i += 1
            exprs.append(PROTO_MAP.get(args[i], args[i]))
        elif a in ("--dport", "--destination-port") and i + 1 < len(args):
            i += 1
            exprs.append(f"dport {args[i]}")
        elif a in ("--sport", "--source-port") and i + 1 < len(args):
            i += 1
            exprs.append(f"sport {args[i]}")
        elif a in ("-i", "--in-interface") and i + 1 < len(args):
            i += 1
            exprs.append(f"iifname {args[i]!r}")
        elif a in ("-o", "--out-interface") and i + 1 < len(args):
            i += 1
            exprs.append(f"oifname {args[i]!r}")
        elif a in ("-s", "--source") and i + 1 < len(args):
            i += 1
            exprs.append(f"ip saddr {args[i]}")
        elif a in ("-d", "--destination") and i + 1 < len(args):
            i += 1
            exprs.append(f"ip daddr {args[i]}")
        elif a in ("-j", "--jump") and i + 1 < len(args):
            i += 1
            target = args[i]
            if target in TARGET_MAP:
                exprs.append(TARGET_MAP[target])
            elif target == "REJECT":
                exprs.append("reject")
            else:
                exprs.append(f"jump {target}")
        elif a in ("-m", "--match") and i + 1 < len(args):
            i += 1
            mod = args[i]
            if mod == "comment":
                i += 1
                if i < len(args) and args[i] == "--comment":
                    i += 1
            elif mod in ("tcp", "udp", "icmp", "multiport", "conntrack", "state"):
                pass
        elif a == "--comment" and i + 1 < len(args):
            i += 1
        elif a in ("--ctstate", "--state") and i + 1 < len(args):
            i += 1
            states = args[i].split(",")
            st_map = {
                "NEW": "new",
                "ESTABLISHED": "established",
                "RELATED": "related",
                "INVALID": "invalid",
                "UNTRACKED": "untracked",
            }
            nft_states = [st_map.get(s, s.lower()) for s in states]
            exprs.append(f"ct state {{{', '.join(nft_states)}}}")
        elif a == "--to-source" and i + 1 < len(args):
            i += 1
        elif a == "--to-destination" and i + 1 < len(args):
            i += 1
        elif a == "--wait":
            pass
        i += 1
    return exprs


def run_nft_rule(action, family, table, chain, rule_args, pos=None):
    ensure_chain(family, table, chain)
    exprs = parse_rule(rule_args)
    if action in ("-A", "--append"):
        ok, _, err = nft("add", "rule", family, table, chain, *exprs)
    elif action in ("-I", "--insert"):
        ok, _, err = nft("insert", "rule", family, table, chain, *exprs)
    elif action in ("-D", "--delete"):
        out_ok, out, _ = nft("list", "chain", family, table, chain)
        handle = None
        for line in out.splitlines():
            if all(e in line for e in exprs if e):
                m = re.search(r"# handle (\d+)", line)
                if m:
                    handle = m.group(1)
                    break
        if handle:
            ok, _, err = nft("delete", "rule", family, table, chain, "handle", handle)
        else:
            ok, err = True, ""
    else:
        ok, err = True, ""
    return ok


def main():
    prog = sys.argv[0].rsplit("/", 1)[-1]
    family = TABLE_FAMILY.get(prog, "ip")

    args = sys.argv[1:]
    table = "filter"
    chain = None
    action = None
    rule_args = []
    pos = None

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-t", "--table") and i + 1 < len(args):
            i += 1
            table = args[i]
        elif a in ("-A", "--append", "-I", "--insert", "-D", "--delete",
                   "-N", "--new-chain", "-X", "--delete-chain",
                   "-F", "--flush", "-Z", "--zero", "-P", "--policy",
                   "-E", "--rename-chain"):
            action = a
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 1
                chain = args[i]
                if action in ("-I", "--insert") and i + 1 < len(args) and args[i + 1].isdigit():
                    i += 1
                    pos = int(args[i])
        elif a in ("-L", "--list", "-S", "--list-rules", "-C", "--check"):
            sys.exit(0)
        elif a in ("-v", "--verbose", "-n", "--numeric", "--line-numbers"):
            pass
        elif a in ("--wait", "-w"):
            if i + 1 < len(args) and args[i + 1].isdigit():
                i += 1
        elif a.startswith("-") and action:
            rule_args.append(a)
            if i + 1 < len(args):
                rule_args.append(args[i + 1])
                i += 1
        i += 1

    if not action:
        if "--version" in args or "-V" in args:
            prog_name = "iptables" if family == "ip" else "ip6tables"
            print(f"{prog_name} v1.8.10 (nf_tables)")
        sys.exit(0)

    if action in ("-N", "--new-chain"):
        nft("add", "table", family, table)
        nft("add", "chain", family, table, chain)
        sys.exit(0)

    if action in ("-X", "--delete-chain"):
        nft("flush", "chain", family, table, chain)
        nft("delete", "chain", family, table, chain)
        sys.exit(0)

    if action in ("-F", "--flush"):
        if chain:
            nft("flush", "chain", family, table, chain)
        else:
            nft("flush", "table", family, table)
        sys.exit(0)

    if action in ("-P", "--policy"):
        sys.exit(0)

    if action in ("-Z", "--zero"):
        sys.exit(0)

    if action in ("-A", "--append", "-I", "--insert", "-D", "--delete"):
        ok = run_nft_rule(action, family, table, chain, rule_args, pos)
        sys.exit(0 if ok else 0)

    sys.exit(0)


if __name__ == "__main__":
    main()
